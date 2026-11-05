# Plan: write `specs/misp-collector.md`

## Context
The user wants a collector that pulls **published** MISP events, converts them to STIX 2.1 with the `misp-stix` library (`MISPtoSTIX21Parser`; `stix2misp` goes the other direction), and pushes them into a configurable bucket the way the TAXII collector does. The deliverable is **only spec documents**, with no code changes.

Decisions confirmed with the user:
- Convert inside the collector with `misp-stix`.
- Gaps in platform type coverage are out of scope and listed as known limitations.
- First run uses an optional `initial_lookback_days`.

This revision incorporates the independent review (`/tmp/claude-1001/-home-daan-workspace-STIXHub/f57d1937-04cf-46e5-afd5-fb45e88a06bb/scratchpad/spec-review.md`).

## Actions
1. Create `specs/misp-collector.md`, using the section layout of `specs/taxii-collector.md`, with the content below.
2. `specs/taxii-collector.md`: add an optional `type: taxii` (the default) to the config example, and remove "MISP" from the non-goals wording, or point it at the new spec.
3. `specs/platform-config.md` line ~31: link both collector specs.

## Spec content

### Overview, goals and non-goals
- Polls one MISP instance and ingests only events where `published = true`. Each event is converted with `MISPtoSTIX21Parser` and submitted to `target_bucket` through the existing `/collectors` API (`src/routes/collectors.py` → `services/ingest.py`).
- Non-goals: writing back to MISP, unpublished events, MISP feeds or ZMQ, filtering or transforms (pipelines do that), and extending the STIX processors.
- Coverage is bounded by what the API key's MISP user can see (distribution and sharing groups). Recommend a dedicated read-only sync user.

### Shared collector refactor (required, not just reuse)
- Today `register()`, `run()`, `run_cycle()`, `_retry`, `RegistrationError` and `REGISTRATION_ATTEMPTS` live in `TaxiiCollector` (`src/collectors/taxii_collector.py`).
  - Move them into `collectors/base.py` as `BaseCollector`, with `poll_once()` abstract.
  - `TaxiiCollector` and `MispCollector` subclass it.
- `CollectorRunner` (`src/collectors/runner.py`) is typed over `BaseCollector`.
- `create_runner` dispatches on `config.type`:
  - It calls `load_credentials` only for TAXII.
  - It builds the MISP `httpx.Client` with the TLS settings and a `timeout`.
- Unchanged and reused as they are: `PlatformClient`, `with_retries`/`is_transient`, `validate_collectors`, `validate_collector_secrets`, and the threading/shutdown/registration semantics from the TAXII spec.

### Fetching (`collectors/misp_client.py`, synchronous httpx, no PyMISP)
- Headers: `Authorization: <api key>`, `Accept: application/json`, `Content-Type: application/json`.
- Response envelope: `{"response": [{"Event": {...}}]}`. MISP serialises numeric fields as strings, so `publish_timestamp` is always `int()`-converted.
- **Phase 1, listing:** `POST /events/restSearch` with `published: true`, `metadata: true`, `publish_timestamp: [window_start, listing_started_at]`, `limit`/`page`, and the optional `tags`/`org`.
  - The upper bound freezes the window, so events published while listing is in progress are picked up next poll.
  - Pages are deduplicated by uuid, sorted on the client by `(int publish_timestamp, uuid)`, and checked for stop between pages.
  - When the start is 0, the `publish_timestamp` lower bound is left out.
- **Phase 2, per event:** `GET /events/view/{uuid}`, which returns exactly one event. The collector checks `Event.uuid` and `Event.published` on its side.
- TLS:
  - When `ca_bundle` is set, `verify=ssl.create_default_context(cafile=ca_bundle)`.
  - When `verify_tls: false`, `verify=False` with a startup warning.
  - Setting both is a config error.
- `timeout` comes from config (default 120s), because MISP events can be large.

### Conversion and submission
- Each event uses a fresh `MISPtoSTIX21Parser()`. The collector calls `parse_misp_event({"Event": ...})`, and the objects are `json.loads(parser.bundle.serialize())["objects"]`.
- A conversion exception or an empty result: log, skip, and advance the cursor.
- Objects are ordered so non-referencing objects come first and `relationship` (any `REF_PROPERTIES` type) comes last. This keeps ref rewriting in `ingest_objects` correct across chunks. They are submitted in chunks of `submit_batch_size` (default 500), which is a safety cap.
- **Acceptance:** any 2xx from `POST /collectors/{id}/objects` counts as accepted, whatever its `success_count`. Per-object rejections (unsupported types) are deterministic and logged, and must never stall the collector. This deliberately differs from TAXII.
- Platform 4xx on a chunk: log loudly, skip the event, and advance the cursor.

### Cursor
- Stored as an opaque JSON string: `{"publish_timestamp": int, "recent": {"<uuid>": <publish_timestamp>, ...}}`.
- `window_start = cursor.publish_timestamp - overlap_seconds` (config, default 3600). The overlap catches events that become visible late or that synced in with older timestamps.
- An event is skipped if `recent[uuid] == its listed publish_timestamp`. `recent` is pruned to entries inside the overlap window.
- The cursor only takes **listing** values and only moves forward: `max(cursor, listed_ts)`. It advances after each event is accepted or skipped.
- An event that is gone or unpublished in phase 2 is skipped with the cursor advanced. A later republish brings it back with a newer timestamp.
- Known gap: an event whose `publish_timestamp` is older than `overlap_seconds` behind the cursor when it first becomes visible is missed. This is documented.
- First run: `window_start = now - initial_lookback_days`, computed once. The cursor is persisted after the first poll even if no events were found, so a restart does not re-apply the lookback.
- An unparseable cursor (for example after switching a collector's `type` from taxii to misp under the same name) is parsed in `register()` and raises `RegistrationError`, which is fatal at startup. The error message tells the operator to reset the cursor.

### Duplicate semantics (stated precisely)
- Restarts and same-second events cause no duplicates.
- Republished events, and events retried after a failure in the middle of an event, are submitted again in full. In **append** buckets this creates duplicates. **Merge-mode target buckets are recommended**, and startup logs a warning when a MISP collector targets an append bucket.

### Error handling
- Transient errors are transport errors, 5xx and **429** (honour `Retry-After`). They are retried with `with_retries`, which needs a small extension to `is_transient` or a MISP-specific predicate.
- 401/403 on the listing means a bad key: log and abort the poll.
- 403/404 on an individual event in phase 2: skip the event, log it, and advance the cursor, so one event cannot block the others.

### Configuration
- `CollectorConfig` stays the exported name. It becomes an annotated union over `TaxiiCollectorConfig | MispCollectorConfig`, with a callable `Discriminator` that returns `"taxii"` when `type` is absent, so existing YAML keeps working. Both variants use `extra="forbid"`. Shared fields go in `BaseCollectorConfig`.
- Code that must keep working:
  - `dependencies.require_collector`
  - `routes/collectors.py`
  - `collectors/validation.py`
  - `tests/unit/test_collector_config.py`

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

- Settings:
  - `Settings.COLLECTOR_API_KEYS: dict[str, SecretStr] = Field(default_factory=dict)`.
  - `load_api_key(config, keys=None)` looks up `api_key_env.lower()`.
  - `validate_collector_api_keys` runs in the lifespan next to `validate_collector_secrets` and fails startup for any `type: misp` collector without a non-empty key.
- Dependency: pinned `misp-stix`. Check that it installs on Python 3.13. It also pulls in STIX 1.x and lxml tooling. Add a mypy override with `ignore_missing_imports` for `misp_stix_converter.*`, `stix2.*` and `pymisp.*`.

### Known limitations (out of scope, follow-up work)
- **Dropped types:** `_PROCESSORS` has 23 types. Types such as these are rejected per object:
  - `file`, `ipv6-addr`, `email-addr`, `email-message`, `network-traffic`
  - `x509-certificate`, `artifact`, `autonomous-system`, `mac-addr`, `mutex`
  - `process`, `software`, `user-account`, `windows-registry-key`, `directory`
  - `sighting`, `x-misp-object`, `x-misp-attribute`
- **ID collisions (data-corrupting):**
  - `report` IDs come from `name` (the event `info`), so unrelated events with generic titles collide.
  - `observed-data` IDs come from `first_observed`, so observed-data objects with the same timestamp collide.
  - `grouping` IDs come from `context`.
  - Fixing these processors is recommended before using the collector in production.
- **Refs not rewritten:**
  - `object_refs` on `report`, `grouping` and `observed-data`
  - `created_by_ref`, `object_marking_refs`, `sighting_of_ref` and `where_sighted_refs`
  - Relationships that point at rejected objects are stored with dangling refs.
- **Memory:** very large events are fetched and converted in memory.

### Acceptance criteria
The checklist mirrors the TAXII spec, with these additions:
- Only published events are ingested.
- Events are processed in order and ingestion resumes correctly after a restart.
- Same-second events are deduplicated across a page boundary.
- An event with a late timestamp inside the overlap window is ingested.
- A chunk with zero successes still advances the cursor.
- An event that vanishes, is unpublished, or is republished between phase 1 and phase 2 is handled as specified.
- A 403/404 on one event skips only that event.
- Relationships are submitted after their targets across chunks.
- TAXII YAML without `type` still validates, and an unknown or typo'd key is rejected.
- A missing MISP API key fails startup.
- Lookback is applied only once.
- An unparseable cursor causes a clear registration failure.
- Tests: unit tests with a mocked httpx MISP, a recorded event fixture for conversion, and an e2e test with a mock MISP modelled on `src/tests/integration/test_taxii_collector_e2e.py`.

## Implementation sub-tasks (one PR or commit each, in this order)
Each task leaves `pytest`, `ruff check src/` and `mypy src/` green.

1. **Spec docs.** Write `specs/misp-collector.md`. Make the small edits to `specs/taxii-collector.md` and `specs/platform-config.md`. No code.
2. **Config union.**
   - In `src/models/domain.py`: `BaseCollectorConfig`, `TaxiiCollectorConfig`, `MispCollectorConfig` (all fields, defaults, `extra="forbid"`, the `verify_tls`/`ca_bundle` conflict check), and `CollectorConfig` as an annotated union whose callable discriminator defaults to taxii.
   - Fix type narrowing in `validation.py` and `runner.py`.
   - Tests in `test_collector_config.py`.
3. **API key settings.** `Settings.COLLECTOR_API_KEYS` in `src/config.py`, plus `load_api_key` and `validate_collector_api_keys` in `collectors/validation.py`. Wire them into the lifespan in `src/server/__main__.py` together with the warning for a MISP collector that targets an append bucket. Unit tests.
4. **BaseCollector refactor (no behaviour change).**
   - Extract registration, the run loop, `run_cycle` and `_retry` from `TaxiiCollector` into `collectors/base.py`.
   - Type `CollectorRunner` over the base class.
   - Add a hook in `register()` for subclass cursor parsing.
   - Existing TAXII tests must pass unchanged.
5. **Retry for 429.** Make 429 transient in `collectors/retry.py` and honour `Retry-After`, capped at `max_delay`. Unit tests.
6. **MispClient.** `collectors/misp_client.py`: `list_published(window_start, window_end, tags, org, page_size, should_stop)` and `get_event(uuid)`, with headers, envelope parsing, `int` timestamps, uuid dedupe and sorting. Tests with `httpx.MockTransport`.
7. **MISP cursor.** `collectors/misp_cursor.py`: a pure dataclass with `parse`/`serialize`, `window_start`, `should_skip`, `advance` (forward only, prunes `recent`) and `initial`. Unit tests, including same-second events and late events inside the overlap window.
8. **Conversion.**
   - Add the pinned `misp-stix` dependency and the mypy overrides.
   - `collectors/misp_convert.py`: `convert_event(event) -> list[dict]` (fresh parser, JSON round-trip, refs last) and `chunk(objects, size)`.
   - Recorded MISP event fixture plus tests (normal event, empty event, parser exception).
9. **MispCollector.** `collectors/misp_collector.py` with `poll_once` that ties tasks 6–8 together:
   - Two phases, with per-event acceptance (any 2xx counts).
   - Skip and advance when an event vanishes, is unpublished, gets a 403/404, or hits a platform 4xx.
   - Abort the poll on 401/403 from the listing call.
   - Persist the cursor once after the first-run lookback.
   - A cursor that fails to parse raises `RegistrationError`.
   - Unit tests with fake clients.
10. **Runner wiring.**
    - `create_runner` dispatches on `type`.
    - Build the MISP `httpx.Client` with an `ssl` context or `verify=False` and the configured timeout, and close it on stop.
    - Add a commented MISP example to `platform_config.yaml`.
    - Tests in `test_collector_runner.py` and `test_startup.py`.
11. **E2E test.** Add `src/tests/integration/test_misp_collector_e2e.py`, an in-process mock MISP that follows the pattern of `test_taxii_collector_e2e.py`. It covers ingestion, restart and resume, and republishing.

Dependencies:
- Tasks 2→3 and 2→4 run in order.
- Tasks 5, 6, 7 and 8 are independent of each other.
- Task 9 needs tasks 4–8.
- Task 10 needs tasks 3 and 9.
- Task 11 needs task 10.

## Execution process (requested by the user)
1. Commit this plan to the repo as `specs/misp-collector-plan.md` on branch `feat/misp-sync`. Do not include the untracked `hello_world.py`.
2. For each task 1→11, strictly in order:
   - **Implementer:** a new Sonnet agent in a Herdr pane (`claude --model sonnet`). It implements the task, runs pytest, ruff and mypy, and commits.
   - **Reviewer:** another new Sonnet agent that reviews that commit against the plan and spec. It writes findings to a scratchpad file.
   - **Fixer:** the reviewer processes its own feedback by applying the fixes, re-running the checks, and committing.
   - **Me:** before starting the next task, I check `git log` and that the tests pass, then close the panes I created.

## Verification
- Read the new spec back against the actual names in the code: `PlatformClient`, `with_retries`, `is_transient`, `Settings.COLLECTOR_SECRETS`, `ingest_objects`, `REF_PROPERTIES`.
- Check that the edits to `specs/taxii-collector.md` and `specs/platform-config.md` are consistent with the new spec.
- There are no code changes, so there are no tests to run.
