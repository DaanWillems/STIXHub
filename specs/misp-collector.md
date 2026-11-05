# MISP Collector

## Overview

A collector that periodically polls one MISP instance, converts each **published** event to STIX 2.1 with the `misp-stix` library (`MISPtoSTIX21Parser`), and pushes the resulting objects into a configurable local bucket. It follows the same model as the [TAXII collector](taxii-collector.md): a background thread, registration with the platform, a persisted cursor, and submission through the `/collectors` API (`src/routes/collectors.py` → `services/ingest.py`).

Coverage is bounded by what the API key's MISP user can see (distribution levels and sharing groups). A dedicated read-only sync user is recommended.

## Goals

- Ingest published MISP events into a bucket automatically.
- Resume correctly after a restart, without missing events or (in most cases) duplicating them.
- Reuse the existing collector infrastructure (`PlatformClient`, retries, registration, shutdown semantics).

## Non-Goals

- Writing back to MISP.
- Unpublished events.
- MISP feeds or ZMQ streaming.
- Filtering or transforming objects — that is the responsibility of pipelines.
- Extending the STIX processors (see Known limitations).

## Functional Requirements

### Shared collector refactor

Reuse is not free: today `register()`, `run()`, `run_cycle()`, `_retry`, `RegistrationError` and `REGISTRATION_ATTEMPTS` live in `TaxiiCollector` (`src/collectors/taxii_collector.py`).

- Move them into `collectors/base.py` as `BaseCollector`, with an abstract `poll_once()`. `TaxiiCollector` and `MispCollector` subclass it.
- `CollectorRunner` (`src/collectors/runner.py`) is typed over `BaseCollector`.
- `create_runner` dispatches on `config.type`. It calls `load_credentials` only for TAXII, and builds the MISP `httpx.Client` with the TLS settings and a `timeout`.
- Unchanged and reused: `PlatformClient`, `with_retries` / `is_transient`, `validate_collectors`, `validate_collector_secrets`, and the threading, shutdown and registration semantics from the TAXII spec.

### Fetching (`collectors/misp_client.py`)

Synchronous `httpx`; PyMISP is not used.

- Headers: `Authorization: <api key>`, `Accept: application/json`, `Content-Type: application/json`.
- Response envelope: `{"response": [{"Event": {...}}]}`. MISP serialises numeric fields as strings, so `publish_timestamp` is always converted with `int()`.

**Phase 1 — listing.** `POST /events/restSearch` with `published: true`, `metadata: true`, `publish_timestamp: [window_start, listing_started_at]`, `limit` / `page`, and the optional `tags` / `org`.

- The upper bound freezes the window, so events published while listing is in progress are picked up by the next poll.
- Pages are deduplicated by uuid, sorted on the client by `(int publish_timestamp, uuid)`, and checked for stop between pages.
- When `window_start` is 0 the lower bound is left out.

**Phase 2 — per event.** `GET /events/view/{uuid}`, which returns exactly one event. The collector checks `Event.uuid` and `Event.published` itself.

**TLS and timeouts.**

- When `ca_bundle` is set: `verify=ssl.create_default_context(cafile=ca_bundle)`.
- When `verify_tls: false`: `verify=False`, with a warning at startup.
- Setting both is a configuration error.
- `timeout` comes from config (default 120 s), because MISP events can be large.

### Conversion and submission

- Each event uses a fresh `MISPtoSTIX21Parser()`. The collector calls `parse_misp_event({"Event": ...})`; the objects are `json.loads(parser.bundle.serialize())["objects"]`.
- A conversion exception or an empty result is logged and skipped, and the cursor advances.
- Objects are ordered so that non-referencing objects come first and `relationship` (any type in `REF_PROPERTIES`) comes last. This keeps ref rewriting in `ingest_objects` correct across chunks.
- Objects are submitted in chunks of `submit_batch_size` (default 500), which is a safety cap.
- **Acceptance:** any 2xx from `POST /collectors/{id}/objects` counts as accepted, whatever its `success_count`. Per-object rejections (unsupported types) are deterministic; they are logged and must never stall the collector. This deliberately differs from the TAXII collector.
- A platform 4xx on a chunk is logged loudly, the event is skipped, and the cursor advances.

### Cursor

Stored as an opaque JSON string: `{"publish_timestamp": int, "recent": {"<uuid>": <publish_timestamp>, ...}}`.

- `window_start = cursor.publish_timestamp - overlap_seconds` (default 3600). The overlap catches events that become visible late or were synced in with older timestamps.
- An event is skipped if `recent[uuid]` equals its listed `publish_timestamp`. `recent` is pruned to entries inside the overlap window.
- The cursor only takes **listing** values and only moves forward: `max(cursor, listed_ts)`. It advances after each event is accepted or skipped.
- An event that is gone or unpublished in phase 2 is skipped with the cursor advanced. A later republish brings it back with a newer timestamp.
- **Known gap:** an event whose `publish_timestamp` is more than `overlap_seconds` behind the cursor when it first becomes visible is missed.
- **First run:** `window_start = now - initial_lookback_days`, computed once. The cursor is persisted after the first poll even if no events were found, so a restart does not re-apply the lookback. If `initial_lookback_days` is unset, all published events are fetched.
- An unparseable cursor (for example after changing a collector's `type` from `taxii` to `misp` under the same name) is detected in `register()` and raises `RegistrationError`, which is fatal at startup. The message tells the operator to reset the cursor.

### Duplicate semantics

- Restarts and events sharing the same second cause no duplicates.
- Republished events, and events retried after a failure in the middle of an event, are submitted again in full. In **append** buckets this creates duplicates.
- **Merge-mode target buckets are recommended.** Startup logs a warning when a MISP collector targets an append bucket.

### Error handling

- Transient errors are transport errors, 5xx, and **429** (honouring `Retry-After`, capped at `max_delay`). They are retried with `with_retries`, which needs a small extension to `is_transient` or a MISP-specific predicate.
- 401/403 on the listing call means a bad key: log and abort the poll.
- 403/404 on an individual event in phase 2: log it, skip that event and advance the cursor, so one event cannot block the others.
- After `max_retries` consecutive failures the collector logs an error and waits for the next scheduled poll, as in the TAXII collector.

### Authentication

- The MISP API key is read from the environment via `Settings.COLLECTOR_API_KEYS: dict[str, SecretStr]` (default empty), i.e. `STIX_HUB_COLLECTOR_API_KEYS__<API_KEY_ENV>`; it never appears in YAML.
- `load_api_key(config, keys=None)` looks up `api_key_env.lower()`.
- `validate_collector_api_keys` runs in the lifespan next to `validate_collector_secrets` and fails startup for any `type: misp` collector without a non-empty key.
- Calls to the platform use the collector's own shared secret (`secret_env`), exactly as in the TAXII collector.

### Configuration (platform_config.yaml)

`CollectorConfig` stays the exported name. It becomes an annotated union of `TaxiiCollectorConfig | MispCollectorConfig`, with a callable `Discriminator` that returns `"taxii"` when `type` is absent, so existing YAML keeps working. Both variants use `extra="forbid"`; shared fields live in `BaseCollectorConfig`. Code that must keep working: `dependencies.require_collector`, `routes/collectors.py`, `collectors/validation.py` and `tests/unit/test_collector_config.py`.

```yaml
collectors:
  - name: circl-misp
    type: misp
    misp_url: https://misp.example.org
    api_key_env: CIRCL_MISP_KEY       # STIX_HUB_COLLECTOR_API_KEYS__CIRCL_MISP_KEY
    secret_env: CIRCL_MISP            # platform API shared secret
    target_bucket: refined-intel      # merge mode recommended
    verify_tls: true
    ca_bundle: null
    timeout: 120
    initial_lookback_days: 30         # unset = all published events
    overlap_seconds: 3600
    tags: ["tlp:green"]               # event-level filter; "!tag" negation allowed
    org: null                         # owner org (id, name or uuid)
    page_size: 100
    submit_batch_size: 500
    poll_interval: 300
    max_retries: 3
    shutdown_timeout: 30
```

### Dependency

A pinned `misp-stix` release. Verify it installs on Python 3.13; it also pulls in STIX 1.x and lxml tooling. Add a mypy override with `ignore_missing_imports` for `misp_stix_converter.*`, `stix2.*` and `pymisp.*`.

## Known limitations (out of scope; follow-up work)

- **Dropped types.** The platform's `_PROCESSORS` covers 23 types. Objects of other types are rejected individually, e.g. `file`, `ipv6-addr`, `email-addr`, `email-message`, `network-traffic`, `x509-certificate`, `artifact`, `autonomous-system`, `mac-addr`, `mutex`, `process`, `software`, `user-account`, `windows-registry-key`, `directory`, `sighting`, `x-misp-object`, `x-misp-attribute`.
- **ID collisions (data-corrupting).**
  - `report` IDs are derived from `name` (the event `info`), so unrelated events with generic titles collide.
  - `observed-data` IDs are derived from `first_observed`, so objects with the same timestamp collide.
  - `grouping` IDs are derived from `context`.
  - Fixing these processors is recommended before using the collector in production.
- **Refs not rewritten.** `object_refs` on `report`, `grouping` and `observed-data`; `created_by_ref`, `object_marking_refs`, `sighting_of_ref` and `where_sighted_refs`. Relationships pointing at rejected objects are stored with dangling refs.
- **Memory.** Very large events are fetched and converted in memory.

## Acceptance Criteria

Mirrors the TAXII collector's checklist (registration, restart safety, secrets from env vars, retries, shutdown, unit and integration tests), plus:

- [ ] Only published events are ingested.
- [ ] Events are processed in order and ingestion resumes correctly after a restart.
- [ ] Same-second events are deduplicated across a page boundary.
- [ ] An event with a late timestamp inside the overlap window is ingested.
- [ ] A chunk with zero successes still advances the cursor.
- [ ] An event that vanishes, is unpublished, or is republished between phase 1 and phase 2 is handled as specified.
- [ ] A 403/404 on one event skips only that event.
- [ ] Relationships are submitted after their targets across chunks.
- [ ] TAXII YAML without `type` still validates, and an unknown or mistyped key is rejected.
- [ ] A missing MISP API key fails startup.
- [ ] The initial lookback is applied only once.
- [ ] An unparseable cursor causes a clear registration failure.
- [ ] Tests: unit tests with a mocked `httpx` MISP, a recorded event fixture for conversion, and an e2e test with an in-process mock MISP modelled on `src/tests/integration/test_taxii_collector_e2e.py`.
