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
      batch_size: 100               # entities read per request (1-1000); default 100
      settle_seconds: 10            # only read entities at least this old; default 10
      wait_for_refs_seconds: 120    # how long a report waits for its objects; default 120
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
| Two names only differ in case or `-` vs `_` | `Duplicate collector name '<name>'` |

Collector names stay unique across both types (the last rule keeps their secret keys
unique, see [Access to the platform](#access-to-the-platform)).

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

### Access to the platform

The internal API (`/collectors/...`, `/misp-sync/...`) is served by the same app as TAXII,
so it can't rely on being reachable from localhost only. Every collector, TAXII or MISP,
shares a **secret** with the platform and sends it as `Authorization: Bearer <secret>` on
every call (`collector_auth.py`):

- The secret comes from `STIX_HUB_COLLECTOR_SECRETS__<NAME>`, with the collector's name
  upper-cased and `-` written as `_` (env var names can't hold `-`). A collector without
  one gets a random secret for the run, shared in-process by the server and the collector
  thread; a collector running elsewhere needs the configured one.
- Each registration is **granted one bucket and one kind of access**, taken from the
  config. Nothing else is reachable:

  | Registration | Bucket | Access |
  |---|---|---|
  | TAXII collector `<name>` | `target_bucket` | write (submit objects) |
  | MISP connector `<name>` (pull) | `pull.target_bucket` | write |
  | MISP connector `<name>:push` | `push.source_bucket` | read (list, look up) |

- A request without the right secret gets **401**. Registering for another bucket, reading
  with a write grant or writing with a read grant gets **403**. A registration whose
  bucket no longer matches the config (the config changed) gets 403 until it registers
  again.
- The sync records of `/misp-sync/{connector}/...` belong to the connector's MISP instance,
  derived from its config; a connector can't name another instance.

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
pulled are stored with `creator = collector:<name>`. A stored cursor the connector can't
parse (for example after renaming a TAXII collector into a MISP connector) is a
registration error.

### Pull

1. **List candidates.** Call MISP's event index (`search_index`) from the cursor on, plus
   `tags` and `org` from the config, sorted ascending by the cursor's field. That field is
   the event's **position**:
   - with `published_only` (the default): `publish_timestamp`, and only published events.
     Publishing moves `publish_timestamp` but not `timestamp`, and editing an event
     unpublishes it, so a draft published after newer events, or a republished edit,
     would be missed by a cursor on `timestamp`.
   - otherwise: `timestamp` (last modified), and every event.

   Follow pages of `page_size` rows until a page comes back short. Sort the rows by
   `(position, uuid)` locally as well.
2. **Cursor.** The cursor is JSON: `{"timestamp": <position>, "uuids": [<event uuids>]}`.
   `uuids` lists the events already ingested at exactly that position. MISP's filters are
   inclusive with one-second resolution, so rows at the cursor position whose UUID is in
   `uuids` are skipped. A new connector (no cursor) starts at
   `now - initial_lookback_days`.
3. **Echo check.** Look up the event's sync record (see [Sync records](#sync-records)). The
   check always compares the modification `timestamp`:
   - origin `stixhub`, same `timestamp` as recorded: STIXHub's own push coming back. Skip
     it, but still advance the cursor past it.
   - origin `stixhub`, no timestamp yet (pending: an add whose outcome was never
     recorded): if STIXHub's organisation created the event in MISP, it is that push;
     record its timestamp and skip it. Otherwise ingest it (MISP owns it).
   - origin `stixhub`, different `timestamp`: a MISP user edited it. Ingest it like any
     other event; step 6 makes MISP its owner.
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
8. **Updated events.** An edited event comes back once it is republished (or right away
   without `published_only`), and is converted again. Its report keeps its platform ID (see
   [Platform ID of containers](#platform-id-of-containers)), so the new version lands as a
   new version of the same objects. Append buckets store it next to the old one; merge
   buckets follow their merge mode.

### Push

1. **Read new entities.** Page through
   `GET /collectors/{push_id}/entities?after=<cursor>&settled_for=<settle_seconds>` in
   `batch_size` steps. A page ends before the first entity younger than `settle_seconds`:
   entity ids are assigned before commit, so a younger row may still have an older
   neighbour being committed, and the cursor must not pass it. (A transaction that takes
   longer than `settle_seconds` can still be skipped.)
2. **Filter.** Skip entities whose `creator` is in the excluded set:
   - `collector:<name>` (this connector's pull)
   - `collector:<other>` for every other MISP connector with the same `url` (normalised: no
     trailing slash, scheme and host lower-cased)
   - everything in `push.exclude_creators`

   Because pipelines keep `creator` (see [pipelines.md](pipelines.md#provenance)), this also
   excludes intel pulled from this MISP and then refined by pipelines.
3. **Pick containers.** Of what's left, only `report` and `grouping` entities produce events,
   and within one page only the newest version of each. Every other entity is either
   pushed as part of a container or skipped (loose objects are a non-goal).
4. **Collect the event's objects.** Starting from the container, follow every top-level ref
   (`*_ref` and `*_refs` properties: `object_refs`, `created_by_ref`,
   `object_marking_refs`, a relationship's ends, a connection's addresses, …) until no new
   refs turn up. Refs are looked up through `POST /collectors/{push_id}/entities/lookup`,
   in the source bucket only; a ref matches an entity's platform ID or one of its
   `other_stix_ids`. A ref to an object already in the bundle (by original or platform ID)
   isn't looked up again.

   The lookup returns the **newest** version (highest `id`) of each object.
   Relationships are included when a collected object references them; misp-stix lists an
   event's relationships in its report's `object_refs`.

   **Waiting for objects.** A pipeline may copy a report into the push bucket before the
   objects it references. If any ref doesn't resolve (well-known TLP markings aside, which
   feeds often reference without sending) and the container is younger than
   `wait_for_refs_seconds`, the container is **deferred**: the cursor stops just before it
   and the push step ends, so it and everything after it are retried next cycle. Once the
   wait is over it is pushed with what resolved, and the missing refs are logged. While a
   container waits, later containers wait with it.
5. **Restore original IDs.** For every object in the bundle, use the first entry of
   `other_stix_ids` as its `id`, and rewrite every ref in the bundle through that
   platform-ID → original-ID map. `misp-stix` gets MISP UUIDs back from STIX IDs, so this is
   what makes a round trip keep the same event, attribute and object UUIDs.
6. **Convert.** Load the bundle with misp-stix's loader. Choose `InternalSTIX2toMISPParser`
   when the container carries misp-stix's `misp:tool="MISP-STIX-Converter"` label (the
   check `is_stix2_from_misp` does, without its warning on every call) and
   `ExternalSTIX2toMISPParser` otherwise. Run it with `single_event=True` and the
   configured `distribution` / `sharing_group_id`. Set the event UUID to the UUID part of
   the container's original ID, so the same report always maps to the same MISP event.
   `timestamp`, `publish_timestamp` and `published` are removed from the event: MISP
   refuses an update that isn't newer than its copy, and publishing follows `publish`.
7. **Upsert.** Decide from the sync record and from MISP:

   | Sync record | Event in MISP | Action |
   |---|---|---|
   | none | absent | Add (see below). |
   | none | present | Somebody else owns it. Skip and log. |
   | origin `stixhub`, pending | present, created by STIXHub's organisation | A previous add reached MISP but wasn't recorded. Adopt it: record its `timestamp`, then `update_event`. |
   | origin `stixhub`, pending | present, created by another organisation | Not STIXHub's. Set origin `misp`, skip. |
   | origin `stixhub`, pending | absent | Add again. |
   | origin `stixhub`, `timestamp` = MISP's | present | `update_event`, record the returned `timestamp`. |
   | origin `stixhub`, `timestamp` ≠ MISP's | present | Edited in MISP. Set origin `misp`, skip, log a conflict. |
   | origin `stixhub` | absent (deleted in MISP) | Skip and log. Deletions aren't propagated. |
   | origin `misp` | any | MISP owns it. Skip. |

   **Adding** writes a pending record (origin `stixhub`, no timestamp) first, then calls
   `add_event` **once**: adding isn't idempotent, so it is never retried blindly. If it
   fails, push reads the event back:
   - absent: nothing was created. A transient failure ends the step (retried next cycle,
     the record stays pending); a refusal skips the container.
   - present and created by STIXHub's organisation (the API user's `org_id`): the add
     worked but its response was lost. Adopt it.
   - present and created by another organisation: set origin `misp` and skip.

   If `publish` is set, publish the event after a successful add or update, then record
   the event's timestamp again, so whatever publishing does to it isn't mistaken for an
   edit in MISP.
8. **Advance the cursor** to the highest entity `id` of the batch once every container in
   it has been handled (pushed, skipped, or failed permanently), or to just before a
   deferred container. A transient MISP failure leaves the cursor where it is, so the
   batch is retried next cycle; a 4xx on one container skips that container only.
9. A newer version of a container (a new entity row with a higher `id`) is pushed again as
   an update, subject to the table above.

### Sync records

STIXHub records which events of each MISP instance it has seen and who owns them. Records
belong to the **instance** (its normalised URL), not to a connector, so all connectors of
one instance share them: a pull-only and a push-only connector on the same MISP don't
echo each other's events.

| Column | Type | Notes |
|---|---|---|
| `id` | int | primary key |
| `instance` | str | the MISP instance's normalised URL |
| `event_uuid` | str | MISP event UUID |
| `origin` | enum `misp` \| `stixhub` | who owns the event |
| `misp_timestamp` | int, nullable | the event's MISP `timestamp` at the last sync; null while an add is pending |
| `updated_at` | timestamptz | |

`(instance, event_uuid)` is unique. Ownership only ever moves from `stixhub` to `misp`,
never back.

### Error handling

- `PyMispClient` wraps PyMISP and turns its results into exceptions. PyMISP often returns
  `{"errors": …}` instead of raising, so the wrapper checks every response. PyMISP
  contacts MISP when it's constructed, so the client creates it lazily on the connector
  thread, where a failure is retried like any other.
- **Transient** (retried with the existing exponential back-off): connection errors,
  timeouts, HTTP 5xx. They surface as `MispUnavailable`, a `retry.TransientError`.
- **Permanent** (logged, the step ends until the next cycle): HTTP 4xx, for example a
  missing permission. A 4xx on one event during push skips that event instead. PyMISP
  reports a rejected API key the same way as an unreachable server when it connects, so
  that is retried as transient.
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
| `artifact` | first of the hashes as for `file`, then `url`, then `payload_bin` |
| `directory` | `path` |
| `mutex` | `name` |
| `software` | `vendor`, `name`, `version` joined (those present) |
| `user-account` | `account_type`, `user_id`, `account_login` joined |
| `windows-registry-key` | `key` |
| `x509-certificate` | first of the hashes, then `serial_number` |
| `network-traffic` | `<src_ref>:<src_port> -> <dst_ref>:<dst_port> <protocols>` (`*` for a missing side) |
| `email-message` | `subject`, else `message_id`, else `from_ref` |
| `sighting` | `sighting_of_ref` (ID from original ID, see above) |
| `grouping` | `name`, else `context` (ID from original ID) |
| `process` | `command_line`, else `pid`, else the original ID (ID from original ID) |
| custom `x-*` | first of `name`, `x_misp_value`, `x_misp_name`, else the original ID |

`StixType` in `models/models.py` gets the new types. Custom `x-*` types are matched by
prefix rather than listed.

### Ref rewriting

Ingest rewrites only the refs that **contribute to an object's platform ID or value**, so the
same fact gets the same ID whatever IDs its source used:

| Type | Ref properties rewritten |
|---|---|
| `relationship` | `source_ref`, `target_ref` (as today) |
| `network-traffic` | `src_ref`, `dst_ref` |
| `email-message` | `from_ref` |

An object is processed after the objects in the same batch that it references (in
dependency order, so a relationship to a connection waits for the connection, which waits
for its addresses). Refs to objects outside the batch are resolved against the bucket
through `other_stix_ids`, and refs found nowhere are kept unchanged.

All other refs keep their **original IDs**, in particular a container's `object_refs`, and
`created_by_ref` / `object_marking_refs` as today. They resolve through `other_stix_ids`
(see [pipelines.md](pipelines.md#ref--value-resolution)). Keeping them matters for push:
one IP address in two MISP events is one platform object, but two MISP attributes with
different UUIDs. A container that still names the original IDs it was published with lets
push find exactly the attribute that belongs to that event, instead of guessing between
copies that share a platform ID.

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

New internal endpoints. Like the existing collector endpoints they require the collector's
secret, and each is limited by the registration's grant (see
[Access to the platform](#access-to-the-platform)):

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/collectors/{id}/entities?after=<int>&limit=<int>&settled_for=<s>` | read | Entities of the collector's bucket after an entity `id`, oldest first, up to the first one younger than `settled_for` seconds, with `id`, `stix_id`, `other_stix_ids`, `creator`, `type`, `platform_created` and `object` |
| POST | `/collectors/{id}/entities/lookup` | read | Body `{"refs": [...]}`; newest entity per ref in the collector's bucket (ref = platform ID or original ID) |
| POST | `/misp-sync/{connector}/events/lookup` | MISP connector | Body `{"event_uuids": [...]}`; the sync records of the connector's MISP instance for those events |
| PUT | `/misp-sync/{connector}/events/{event_uuid}` | MISP connector | Create or update a sync record (`origin`, `misp_timestamp`) of the connector's instance |

`POST /collectors/{id}/objects` needs write access; `PUT /collectors/{id}/cursor` needs
either.

`PUT /misp-sync/...` refuses to change origin from `misp` to `stixhub` (HTTP 409).

## Architecture Notes

```
collectors/
  runner.py            # runs anything that matches the Collector protocol
  misp/
    client.py          # MispApi protocol; PyMispClient: typed wrapper over PyMISP
    convert.py         # misp-stix wrappers: event -> STIX objects, bundle -> event dict
    common.py          # retries, registration and cursor loading shared by both steps
    puller.py          # pull step, PullCursor
    pusher.py          # push step, ref collection, ID restoration, ownership rules
    connector.py       # MispConnector: registration, cycle loop
```

- `CollectorRunner` runs any object with `config.name`, `config.shutdown_timeout` and
  `run(on_fatal)` (a `Protocol`). `create_runner` builds a `TaxiiCollector` or a
  `MispConnector` based on `config.type`.
- `collectors` in `PlatformConfig` becomes `list[TaxiiCollectorConfig | MispConnectorConfig]`,
  told apart by `type`. A TAXII entry without `type` defaults to `taxii`.
- `retry.is_transient` also treats `retry.TransientError` as transient. PyMISP uses
  `requests` rather than `httpx`, so `PyMispClient` turns its connection errors into one.
- Only `collectors/misp/client.py` imports PyMISP and only `collectors/misp/convert.py`
  imports misp-stix. The rest of the code works with plain dicts and our own types, which
  keeps the untyped library behind a typed boundary for `mypy --strict`.

### Dependencies

`misp-stix` is a core dependency, pinned to the current calendar year, because its mapping
output can change between releases:

```toml
dependencies = [
    ...
    "misp-stix>=2026.10,<2027",
    "pymisp>=2.5.34.1,<3",
]
```

`pymisp` is listed directly because the connector imports it; its range matches what
`misp-stix` requires, and it ships type information. `uv.lock` is updated with them. mypy
gets an `ignore_missing_imports` override for `misp_stix_converter`. Moving the pin is a
deliberate change, checked by the round-trip tests.

## Acceptance Criteria

### Platform

- [x] `report`, `grouping`, `note`, `opinion`, `observed-data`, `sighting`, `process` and
      `x-*` objects get `uuid5(type, original_id)` as platform ID; their `value` is
      unchanged.
- [x] Every type in the processor coverage table can be ingested.
- [x] Ingest rewrites the ref properties in the ref rewriting table to platform IDs, in
      dependency order, and leaves other and unknown refs unchanged.
- [x] Custom types up to 128 characters long can be stored.
- [x] The new endpoints and repository methods work with both the database and the
      in-memory repositories.
- [x] Every internal API call needs the collector's own secret, and a collector can only
      use the bucket its config grants it, with the access its config implies.

### Pull

- [x] A new connector ingests events from the last `initial_lookback_days` days.
- [x] Each event is submitted as one batch, with `creator = collector:<name>`.
- [x] A restart doesn't re-ingest events; events sharing a timestamp at the cursor are
      neither skipped nor ingested twice.
- [x] An edited event is ingested again, and its report keeps its platform ID.
- [x] With `published_only`, an event published after newer events is still pulled.
- [x] `published_only`, `tags` and `orgs` filters are applied.
- [x] An event that fails to convert is logged and skipped; the cycle continues.
- [x] An event STIXHub pushed is not pulled back unless it was edited in MISP afterwards,
      also by another connector of the same MISP instance.

### Push

- [x] A report or grouping in the source bucket becomes one MISP event, with everything it
      references directly or indirectly (objects, relationships, authors, markings).
- [x] The MISP event's UUID is derived from the container's original ID; pushing again
      updates the same event instead of creating a new one.
- [x] An event pulled from MISP, converted and pushed back produces the same event,
      attribute and object UUIDs.
- [x] Loose objects are not pushed.
- [x] Entities created by this connector's pull, by another connector on the same MISP URL,
      or by a creator in `exclude_creators` are not pushed.
- [x] Events STIXHub didn't create are never modified.
- [x] An event STIXHub created and a MISP user edited afterwards becomes MISP-owned and is
      not updated again.
- [x] A crash between `add_event` and recording its timestamp, or a lost `add_event`
      response, doesn't create a duplicate, lose ownership or take over another
      organisation's event.
- [x] A report whose objects are still on their way waits for them (up to
      `wait_for_refs_seconds`), and the cursor never passes an entity younger than
      `settle_seconds`.
- [x] `distribution`, `sharing_group_id` and `publish` are applied.

### Operation

- [x] Config errors in the validation table stop startup with the given error.
- [x] The API key is read from `STIX_HUB_MISP_CREDENTIALS__<ENV>__API_KEY` and never
      logged.
- [x] Transient MISP and platform errors are retried with back-off; permanent ones are
      logged and don't stop later cycles.
- [x] Pull and push run on one thread per connector and stop within `shutdown_timeout`.
- [x] Existing TAXII collector configs keep working unchanged.

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
- **Conversion fixtures:** a sample MISP event (`tests/fixtures/misp/event.json`:
  attributes, objects, TLP tag; published and unpublished) converted with the real
  `misp-stix`, and every resulting object ingested without failures.
- **Round trip:** fixture event → pull into an in-memory bucket → copy into the push bucket
  → push into another fake MISP → same event, attribute and object UUIDs as the fixture.
- **End to end:** like `test_taxii_collector_e2e.py`. `FakeMisp` (`tests/misp_support.py`)
  plus the real collector routes and in-memory repositories, run for several cycles in
  both directions, checking that nothing is duplicated and nothing echoes.
- **Integration (PostgreSQL):** the new repository methods, the sync table and storing a
  long custom type name.
- **Manual (not in CI):** a pull/push run against a real MISP instance (for example the
  MISP Docker images) whenever the misp-stix or PyMISP pin moves.

## Delivery Order

1. Platform ID of containers, processor coverage, ref rewriting and the wider `type` column.
2. Repository methods, sync records and the internal endpoints.
3. Config models and validation, credentials, the type-agnostic runner and dependencies.
4. `MispClient`, conversion wrappers and the pull step.
5. The push step.
6. Round-trip and end-to-end tests; README, CLAUDE.md and example `platform_config.yaml`.
