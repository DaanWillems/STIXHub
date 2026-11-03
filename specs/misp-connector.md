# MISP Connector

## Overview

A connector that synchronises STIXHub with a MISP instance in both directions:

- **Pull**: poll MISP for new and changed events, convert each one to STIX 2.1 with
  [`misp-stix`](https://github.com/MISP/misp-stix) and write the objects to a bucket, the
  same way the [TAXII collector](taxii-collector.md) does.
- **Push**: read new entities from a bucket, convert each STIX report / grouping (with the
  objects it references) to a MISP event with `misp-stix` and create or update that event in
  MISP.

Both directions can run against the same MISP instance without echoing intel back and forth.

The connector is a collector: it runs on a thread inside the server process, registers with
the platform and talks to it only through the internal REST API. Everything in
[taxii-collector.md](taxii-collector.md) about threading, shutdown, retries and the
extraction seam applies unless this spec says otherwise.

## Goals

- Ingest MISP events into a bucket automatically, keeping a cursor so restarts don't
  re-ingest.
- Ingest updated MISP events again, so the bucket follows changes made in MISP.
- Publish STIX reports and groupings from a bucket to MISP as events, and keep them updated
  while STIXHub owns them.
- Never loop: intel pulled from a MISP instance is not pushed back to it, and events
  STIXHub pushed are not pulled back as new intel.
- Never overwrite an event STIXHub doesn't own.

## Non-Goals

- **Loose objects.** Push only sends reports and groupings, plus the objects they reference.
  Objects in the source bucket that no report or grouping references are skipped. Wrapping
  them in generated events may come later.
- **Writing back to MISP-owned events.** Pipeline enrichments (labels, markings, confidence)
  of intel pulled from MISP are not sent back. Pushing them as additive MISP tags may come
  later.
- **Deletions.** Deleted MISP attributes are excluded from pull, but deletions and
  revocations are not propagated in either direction.
- Real-time feeds (ZMQ, Kafka) and MISP-to-MISP sync semantics. Polling only.
- Separate handling of sightings, proposals or galaxies. They are carried as far as
  `misp-stix` converts them, and no further.

## User Stories

1. **As an analyst**, I want MISP events from my community to appear in a bucket so I can
   filter, refine and share them with STIXHub pipelines and TAXII collections.
2. **As an analyst**, I want reports collected from TAXII feeds or written to a TAXII
   collection to show up as events in our MISP instance.
3. **As an operator**, I want to pull from and push to the same MISP instance without
   duplicating events or creating a sync loop.
4. **As an operator**, I want a MISP user's edits to an event STIXHub created to win over
   STIXHub's later updates, so nobody's work is overwritten.

## Configuration

MISP connectors are entries in the existing `collectors` list with `type: misp`. Entries
without a `type` are TAXII collectors, so existing configs keep working.

```yaml
collectors:
  - name: circl-misp                # required; [A-Za-z0-9_-], used for registration
    type: misp                      # required for MISP connectors
    url: https://misp.example.org   # required; MISP base URL
    credentials_env: CIRCL_MISP     # required; see Authentication
    verify_tls: true                # true | false | path to a CA bundle; default true
    poll_interval: 300              # seconds between cycles; default 300
    max_retries: 3                  # default 3
    shutdown_timeout: 30            # seconds; default 30

    pull:                           # optional; omit to disable pull
      target_bucket: raw-intel      # required; must exist in buckets
      published_only: true          # default true
      initial_lookback_days: 30     # backfill window for a new connector; default 30
      tags: ["tlp:green", "!internal"]   # optional; MISP tag filter syntax
      orgs: []                      # optional; only events created by these orgs
      page_size: 100                # index rows per MISP request; default 100

    push:                           # optional; omit to disable push
      source_bucket: refined-intel  # required; must exist in buckets
      exclude_creators: []          # optional; extra creators to never push
      distribution: 0               # MISP distribution for new events (0-5); default 0
      sharing_group_id: null        # required when distribution is 4
      publish: false                # publish events after create/update; default false
      batch_size: 100               # entities read from the bucket per request; default 100
```

### Validation

Startup fails with a clear error (the same as for TAXII collectors) if any of these hold:

| Condition | Error |
|---|---|
| Neither `pull` nor `push` is set | `MISP connector '<name>' has neither pull nor push configured` |
| `pull.target_bucket` or `push.source_bucket` is not a declared bucket | `MISP connector '<name>' references unknown bucket '<b>'` |
| `pull.target_bucket == push.source_bucket` | `MISP connector '<name>' pulls into and pushes from the same bucket '<b>'` |
| `distribution: 4` without `sharing_group_id` | `MISP connector '<name>': distribution 4 requires sharing_group_id` |
| `name` contains characters outside `[A-Za-z0-9_-]` | `Invalid collector name '<name>'` |
| Credentials are missing | as for TAXII collectors, naming the env var to set |

Collector names stay unique across both types.

### Authentication

MISP uses an API key, not HTTP Basic. Keys are loaded through `Settings`, with the same
`STIX_HUB_` prefix and `.env` support as the TAXII collector credentials:

```python
class MispCredentials(BaseModel):
    api_key: SecretStr

class Settings(BaseSettings):
    ...
    MISP_CREDENTIALS: dict[str, MispCredentials] = Field(default_factory=dict)
```

With `credentials_env: CIRCL_MISP` the connector reads
`STIX_HUB_MISP_CREDENTIALS__CIRCL_MISP__API_KEY`. The key must never appear in
`platform_config.yaml` or in logs.

## Functional Requirements

### Threading and lifecycle

- Each MISP connector runs on one daemon thread, like a TAXII collector. Every cycle runs the
  pull step (if configured) and then the push step (if configured), then waits
  `poll_interval`. Running both steps on one thread means pull and push never race on the
  same connector's sync records.
- A failing step is logged and does not stop the other step or later cycles.
- Shutdown, `shutdown_timeout` and the fatal-error handling on registration failure are the
  same as for TAXII collectors.

### Registration

A MISP connector registers once per direction through the existing
`POST /collectors/register`:

| Direction | Registered name | Bucket | `cursor` holds |
|---|---|---|---|
| Pull | `<name>` | `pull.target_bucket` | the pull cursor (see below) |
| Push | `<name>:push` | `push.source_bucket` | the highest entity `id` already pushed |

That keeps one cursor per registration and needs no new cursor storage. Collector names
can't contain `:`, so `<name>:push` never clashes with a configured collector. Objects
pulled are stored with `creator = collector:<name>`.

### Pull

1. **List candidates.** Call MISP's event index (`search_index`) with
   `timestamp >= cursor.timestamp`, plus `published`, `tags` and `org` from the config.
   Follow pages of `page_size` rows until a page comes back short. Sort the rows by
   `(timestamp, uuid)` locally, so server-side ordering doesn't matter.
2. **Cursor.** The cursor is JSON: `{"timestamp": <unix seconds>, "uuids": [<event uuids>]}`.
   `uuids` lists the events already ingested at exactly `timestamp`. MISP's `timestamp`
   filter is inclusive with one-second resolution, so rows at the cursor timestamp whose
   UUID is in `uuids` are skipped. A new connector (no cursor) starts at
   `now - initial_lookback_days`.
3. **Echo check.** Look up the event's sync record (see [Sync records](#sync-records)):
   - origin `stixhub`, same `timestamp` as recorded: this is STIXHub's own push coming back.
     Skip it, but still advance the cursor past it.
   - origin `stixhub`, different `timestamp`: a MISP user edited it. Set the record's origin
     to `misp` (MISP now owns the event) and ingest it like any other event.
   - no record, or origin `misp`: ingest it.
4. **Fetch and convert.** Fetch the full event (`get_event`, deleted attributes excluded)
   and convert it with a **new** `MISPtoSTIX21Parser` per event (the parser keeps state
   between calls). The result is the event's report (published) or grouping (unpublished),
   its identity, markings, and the objects for its attributes, objects and galaxies.
5. **Submit.** Submit the event's objects as one batch to `POST /collectors/{id}/objects`.
   One event equals one batch.
6. **Record.** Upsert the sync record with origin `misp` (unless the event was recorded as
   origin `stixhub` with an unchanged timestamp, which step 3 already skipped) and the event's
   `timestamp`.
7. **Advance the cursor** after the batch is accepted, as for TAXII:
   - The cursor advances when at least one object was accepted. Failed objects are logged and
     dropped.
   - If the platform accepts none, the cursor stays put and the cycle stops, so the event is
     retried next cycle.
   - A conversion error is a permanent failure for that event: it is logged with the event
     UUID and traceback, and the cursor moves past it.
8. **Updated events.** An edited event has a newer `timestamp`, so it's picked up and
   converted again. Its report keeps its platform ID (see
   [Platform ID of containers](#platform-id-of-containers)), so the new version lands as a
   new version of the same objects. Append buckets store it next to the old one; merge
   buckets follow their merge mode.

### Push

1. **Read new entities.** Page through `GET /collectors/{push_id}/entities?after=<cursor>`
   in `batch_size` steps.
2. **Filter.** Skip entities whose `creator` is in the excluded set:
   - `collector:<name>` (this connector's pull)
   - `collector:<other>` for every other MISP connector with the same `url` (normalised: no
     trailing slash, scheme and host lower-cased)
   - everything in `push.exclude_creators`

   Because pipelines keep `creator` (see [pipelines.md](pipelines.md#provenance)), this also
   excludes intel pulled from this MISP and then refined by pipelines.
3. **Pick containers.** Of what's left, only `report` and `grouping` entities produce events.
   Every other entity is either pushed as part of a container or skipped (loose objects are
   a non-goal).
4. **Collect the event's objects.** For each container, look up through
   `POST /collectors/{push_id}/entities/lookup`, in the source bucket only:
   - the objects in its `object_refs`
   - their `created_by_ref` and `object_marking_refs`
   - relationships whose `source_ref` and `target_ref` are both in the container

   The lookup returns the **newest** version (highest `id`) of each object. Refs that
   don't resolve are dropped from the bundle and logged. They are not retried.
5. **Restore original IDs.** For every object in the bundle, use the first entry of
   `other_stix_ids` as its `id`, and rewrite every ref in the bundle through that
   platform-ID → original-ID map. `misp-stix` gets MISP UUIDs back from STIX IDs, so this is
   what makes a round trip keep the same event, attribute and object UUIDs.
6. **Convert.** Parse the bundle with `stix2` (`allow_custom=True`). Use `is_stix2_from_misp`
   to choose `InternalSTIX2toMISPParser` (bundle produced by MISP) or
   `ExternalSTIX2toMISPParser` (anything else). Run it with `single_event=True` and the
   configured `distribution` / `sharing_group_id`. Set the event UUID to the UUID part of
   the container's original ID, so the same report always maps to the same MISP event.
7. **Upsert.** Decide from the sync record and from MISP:

   | Sync record | Event in MISP | Action |
   |---|---|---|
   | none | absent | Write a record (origin `stixhub`, `timestamp` null = pending), then `add_event`, then record the returned `timestamp`. |
   | none | present | Somebody else owns it. Skip and log. |
   | origin `stixhub`, pending | present | A previous add finished but the record wasn't updated (crash). Adopt it: record its `timestamp`, then continue as the row below. |
   | origin `stixhub`, pending | absent | Retry `add_event`. |
   | origin `stixhub`, `timestamp` = MISP's | present | `update_event`, record the returned `timestamp`. |
   | origin `stixhub`, `timestamp` ≠ MISP's | present | Edited in MISP. Set origin `misp`, skip, log a conflict. |
   | origin `stixhub` | absent (deleted in MISP) | Skip and log. Deletions aren't propagated. |
   | origin `misp` | any | MISP owns it. Skip. |

   If `publish` is set, publish the event after a successful add or update.
8. **Advance the cursor** to the highest entity `id` of the batch once every container in
   it has been handled (pushed, skipped, or failed permanently). A transient MISP failure
   leaves the cursor where it is, so the batch is retried next cycle.
9. A newer version of a container (a new entity row with a higher `id`) is pushed again as
   an update, subject to the table above.

### Sync records

Each connector records which MISP events it has seen and who owns them:

| Column | Type | Notes |
|---|---|---|
| `id` | int | primary key |
| `connector` | str | the connector's configured `name` |
| `event_uuid` | str | MISP event UUID |
| `origin` | enum `misp` \| `stixhub` | who owns the event |
| `misp_timestamp` | int, nullable | the event's MISP `timestamp` at the last sync; null while an add is pending |
| `updated_at` | timestamptz | |

`(connector, event_uuid)` is unique. Ownership only ever moves from `stixhub` to `misp`,
never back.

### Error handling

- `MispClient` wraps PyMISP and turns its results into exceptions. PyMISP often returns
  `{"errors": …}` instead of raising, so the wrapper checks every response.
- **Transient** (retried with the existing exponential back-off): connection errors,
  timeouts, HTTP 5xx.
- **Permanent** (logged, the step ends until the next cycle): HTTP 4xx, for example a bad
  API key or a missing permission. A 403 on one event during push skips that event instead.
- After `max_retries`, the step logs an error and the connector waits for the next cycle.

## Platform Changes

### Platform ID of containers

Today `report` and `grouping` IDs are derived from their `name` / `context`, and
`observed-data` from `first_observed`. That merges unrelated objects: every unpublished MISP
event becomes a grouping with `context: suspicious-activity`, so all of them would share one
ID. It also gives a report a new ID whenever its title changes.

From now on, these types get their platform ID from their **original ID**:

```python
platform_id = _uuid5(obj_type, {"original_id": raw["id"]})
```

| Applies to |
|---|
| `report`, `grouping`, `note`, `opinion`, `observed-data`, `sighting`, `process`, every custom `x-*` type |

The ID stays deterministic (ingesting the same object again gives the same ID), is unique per
original object and doesn't change when the object is edited. `value` doesn't change: it's
still the name / context / abstract etc., so search and pipeline conditions keep working.
The original ID stays in `other_stix_ids`.

There is no migration: existing databases are recreated (see [Database](#database)).

### Processor coverage

`processors/stix.py` gets processors for every type `misp-stix` emits that it doesn't
handle yet. SCO values follow the STIX 2.1 ID-contributing properties:

| Type | Value |
|---|---|
| `ipv6-addr`, `mac-addr`, `email-addr` | `value` |
| `autonomous-system` | `number` |
| `file` | first of `hashes.SHA-256`, `hashes.SHA-1`, `hashes.MD5`, then `name` |
| `artifact` | first of the hashes as for `file`, then `payload_bin` |
| `directory` | `path` |
| `mutex` | `name` |
| `software` | `name`, plus `version` / `vendor` when present |
| `user-account` | `account_type`, `user_id`, `account_login` joined |
| `windows-registry-key` | `key` |
| `x509-certificate` | first of the hashes, then `serial_number` |
| `network-traffic` | `src_ref`, `dst_ref`, `src_port`, `dst_port`, `protocols` joined |
| `email-message` | `subject`, else `from_ref` |
| `sighting` | `sighting_of_ref` (ID from original ID, see above) |
| `process` | `command_line`, else `pid`, else the original ID (ID from original ID) |
| custom `x-*` | first of `name`, `x_misp_value`, `x_misp_name`, else the original ID |

`StixType` in `models/models.py` gets the new types. Custom `x-*` types are matched by
prefix rather than listed.

### Ref rewriting

`REF_PROPERTIES` only rewrites a relationship's `source_ref` / `target_ref` today. Ingest
rewrites these refs to platform IDs too, so containers and SCOs keep pointing at objects in
the bucket:

| Type | Ref properties |
|---|---|
| `report`, `grouping`, `note`, `opinion`, `observed-data` | `object_refs` (list) |
| `sighting` | `sighting_of_ref`, `observed_data_refs`, `where_sighted_refs` |
| `network-traffic` | `src_ref`, `dst_ref` |
| `email-message` | `from_ref`, `to_refs`, `cc_refs`, `bcc_refs` |
| `file` | `parent_directory_ref`, `contains_refs` |

`REF_PROPERTIES` grows to support list-valued properties. Objects with refs are still
processed after the objects they point at, and refs that point at nothing known are kept
unchanged. `created_by_ref` and `object_marking_refs` are **not** rewritten; ref resolution
for pipelines already covers them through `other_stix_ids` (see
[pipelines.md](pipelines.md#ref--value-resolution)).

### Database

No migrations and no `ALTER TABLE`: the schema still comes only from `create_all`, and
existing databases are dropped and recreated after upgrading.

- New table for sync records (see above).
- `stix_entity_model.type` is declared as `String(128)` instead of `String(30)`, for custom
  type names.
- New `BucketRepository` methods, in both the database and in-memory implementations:
  - `get_entities_after(bucket_id, after_id, limit)`: entities with `id > after_id`, by `id`.
  - `find_latest_by_refs(bucket_id, refs)`: like `find_by_refs`, but across all types (the
    type is taken from the ref's prefix) and the **newest** match wins.
- New `MispSyncRepository` (database and in-memory) for sync records.

## Platform API Requirements

New internal endpoints, unauthenticated like the existing collector API (localhost /
internal network only):

| Method | Path | Purpose |
|---|---|---|
| GET | `/collectors/{id}/entities?after=<int>&limit=<int>` | Entities of the collector's bucket after an entity `id`, oldest first, with `id`, `stix_id`, `other_stix_ids`, `creator`, `type` and `object` |
| POST | `/collectors/{id}/entities/lookup` | Body `{"refs": [...]}`; newest entity per ref in the collector's bucket (ref = platform ID or original ID) |
| POST | `/misp-sync/{connector}/events/lookup` | Body `{"event_uuids": [...]}`; sync records for those events |
| PUT | `/misp-sync/{connector}/events/{event_uuid}` | Create or update a sync record (`origin`, `misp_timestamp`) |

`PUT /misp-sync/...` refuses to change origin from `misp` to `stixhub` (HTTP 409).

## Architecture Notes

```
collectors/
  runner.py            # runs anything that matches the Collector protocol
  misp/
    client.py          # MispClient: typed wrapper over PyMISP, raises on errors
    convert.py         # misp-stix wrappers: event -> STIX objects, bundle -> MISPEvent
    puller.py          # pull step
    pusher.py          # push step, ID restoration
    connector.py       # MispConnector: registration, cycle loop
```

- `CollectorRunner` runs any object with `config.name`, `config.shutdown_timeout` and
  `run(on_fatal)` (a `Protocol`). `create_runner` builds a `TaxiiCollector` or a
  `MispConnector` based on `config.type`.
- `collectors` in `PlatformConfig` becomes `list[TaxiiCollectorConfig | MispConnectorConfig]`,
  told apart by `type`. A TAXII entry without `type` defaults to `taxii`.
- `retry.is_transient` also treats `requests` connection errors and timeouts as transient,
  since PyMISP uses `requests` rather than `httpx`.
- Only `collectors/misp/client.py` and `collectors/misp/convert.py` import PyMISP,
  `misp-stix` or `stix2`. The rest of the code works with plain dicts and our own types,
  which keeps the untyped libraries behind a typed boundary for `mypy --strict`.

### Dependencies

`misp-stix` is a core dependency, pinned to the current calendar year, because its mapping
output can change between releases:

```toml
dependencies = [
    ...
    "misp-stix>=2026.10,<2027",
    "pymisp>=2.5.34.1,<3",
    "stix2>=3.0.2,<4",
]
```

`pymisp` and `stix2` are listed directly because the connector imports them. Their ranges
match what `misp-stix` requires. `uv.lock` is updated with them. mypy gets
`ignore_missing_imports` overrides for `misp_stix_converter` and `stix2` (and for `pymisp`
if its type information isn't enough). Moving the pin is a deliberate change, checked by the
round-trip tests.

## Acceptance Criteria

### Platform

- [ ] `report`, `grouping`, `note`, `opinion`, `observed-data`, `sighting`, `process` and
      `x-*` objects get `uuid5(type, original_id)` as platform ID; their `value` is
      unchanged.
- [ ] Every type in the processor coverage table can be ingested.
- [ ] Ingest rewrites the ref properties in the ref rewriting table to platform IDs, and
      leaves unknown refs unchanged.
- [ ] Custom types up to 128 characters long can be stored.
- [ ] The new endpoints and repository methods work with both the database and the
      in-memory repositories.

### Pull

- [ ] A new connector ingests events from the last `initial_lookback_days` days.
- [ ] Each event is submitted as one batch, with `creator = collector:<name>`.
- [ ] A restart doesn't re-ingest events; events sharing a timestamp at the cursor are
      neither skipped nor ingested twice.
- [ ] An edited event is ingested again, and its report keeps its platform ID.
- [ ] `published_only`, `tags` and `orgs` filters are applied.
- [ ] An event that fails to convert is logged and skipped; the cycle continues.
- [ ] An event STIXHub pushed is not pulled back unless it was edited in MISP afterwards.

### Push

- [ ] A report or grouping in the source bucket becomes one MISP event, with the objects it
      references, their authors and markings, and the relationships between them.
- [ ] The MISP event's UUID is derived from the container's original ID; pushing again
      updates the same event instead of creating a new one.
- [ ] An event pulled from MISP, converted and pushed back produces the same event,
      attribute and object UUIDs.
- [ ] Loose objects are not pushed.
- [ ] Entities created by this connector's pull, by another connector on the same MISP URL,
      or by a creator in `exclude_creators` are not pushed.
- [ ] Events STIXHub didn't create are never modified.
- [ ] An event STIXHub created and a MISP user edited afterwards becomes MISP-owned and is
      not updated again.
- [ ] A crash between `add_event` and recording its timestamp doesn't create a duplicate or
      lose ownership.
- [ ] `distribution`, `sharing_group_id` and `publish` are applied.

### Operation

- [ ] Config errors in the validation table stop startup with the given error.
- [ ] The API key is read from `STIX_HUB_MISP_CREDENTIALS__<ENV>__API_KEY` and never
      logged.
- [ ] Transient MISP and platform errors are retried with back-off; permanent ones are
      logged and don't stop later cycles.
- [ ] Pull and push run on one thread per connector and stop within `shutdown_timeout`.
- [ ] Existing TAXII collector configs keep working unchanged.

## Testing

No test needs network access or a real MISP instance.

- **Unit tests:**
  - processors, container IDs and ref rewriting
  - pull cursor and timestamp tie-breaking
  - echo and ownership decisions (every row of the upsert table)
  - creator exclusion
  - original-ID restoration
  - container collection
  - retry classification
  - config validation
- **Conversion fixtures:** sample MISP events (attributes, objects, galaxies, TLP tags,
  published and unpublished) converted with the real `misp-stix`.
- **Round trip:** fixture event → pull into an in-memory bucket → copy into the push bucket
  → push into a `FakeMispClient` → same event, attribute and object UUIDs as the fixture.
- **End to end:** like `test_taxii_collector_e2e.py`. A `FakeMispClient` plus the real
  collector routes and in-memory repositories, run for several cycles in both directions,
  checking that nothing is duplicated and nothing echoes.
- **Integration (PostgreSQL):** the new repository methods, the sync table and storing a
  long custom type name.
- **Manual (documented, not in CI):** a pull/push run against a MISP instance in Docker.

## Delivery Order

1. Platform ID of containers, processor coverage, ref rewriting and the wider `type` column.
2. Repository methods, sync records and the internal endpoints.
3. Config models and validation, credentials, the type-agnostic runner and dependencies.
4. `MispClient`, conversion wrappers and the pull step.
5. The push step.
6. Round-trip and end-to-end tests; README, CLAUDE.md and example `platform_config.yaml`.
