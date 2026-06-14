# TAXII Collector

## Overview

A collector service that periodically scrapes a remote TAXII 2.1 collection and pushes the retrieved STIX objects into a designated local bucket. This implements the "Collector service to read external TAXII 2.1 endpoints" item from the product roadmap.

## Goals

- Allow STIXHub to ingest CTI from external TAXII 2.1 servers automatically.
- Persist a cursor (bookmark) so each run only fetches new objects since the last successful poll.
- Run as a background thread inside the FastAPI process, with a clean seam to extract to a separate service later.
- Configured via `platform_config.yaml`.

## Non-Goals

- Support for non-TAXII ingestion protocols (MISP, CSV, etc.) — those are separate collectors.
- Real-time streaming; polling on a configurable interval is sufficient.
- Transformation or filtering of objects before insertion — that is the responsibility of pipelines.
- Runtime reconfiguration of poll interval or other settings — a restart is required.

## User Stories

1. **As an analyst**, I want to configure STIXHub to pull from a trusted external TAXII feed so that new threat intelligence appears in my bucket automatically without manual uploads.
2. **As an operator**, I want the collector to remember where it left off so that a restart does not re-ingest all historical data.
3. **As an operator**, I want to point multiple collectors at different remote collections and different local buckets independently.

## Functional Requirements

### Threading model

- Each configured collector runs as a `threading.Thread` started during the FastAPI application lifespan (alongside `create_tables` / `dispose` in `__main__.py`).
- All collector code is synchronous; it does not participate in the asyncio event loop.
- On shutdown, a `threading.Event` is set to signal each thread to stop. The lifespan waits up to `shutdown_timeout` seconds (default: 30) for each thread to finish before abandoning it with a warning.

### Registration

- On startup the collector calls `POST /collectors/register` with its name and target bucket name.
- The platform returns the collector's assigned ID and its current cursor (empty for a new collector).
- If registration fails (e.g. the target bucket does not exist) the application crashes with a clear error message — this is a configuration error, not a transient failure.
- The target bucket **must** already be declared in the `buckets` section of `platform_config.yaml`; the platform does not auto-provision buckets on registration.

### Polling

- The collector polls the remote TAXII collection at a configurable interval (`poll_interval`, default: 300 seconds).
- Each poll uses the TAXII `added_after` parameter set to the current cursor value to retrieve only new objects.
- The collector drains all pages in a single poll cycle: it follows `next` tokens until `more` is `false`, submitting each page as one batch before fetching the next.
- After each batch is successfully accepted by the platform the cursor is advanced to the value of the `X-TAXII-Date-Added-Last` response header from that TAXII page. If the header is absent, the current UTC time is used as a fallback.
- The cursor is only advanced after a batch is accepted by the platform — a network failure before submission leaves the cursor unchanged.
- On partial batch failure (`status: "complete_with_errors"`), the cursor is still advanced provided at least one object was accepted. Failed objects are logged and dropped — they are not retried.

### Authentication to remote TAXII server

- Supports HTTP Basic authentication (username + password).
- Credentials are read from environment variables at startup; they must not appear in `platform_config.yaml`.
- Each collector entry declares a `credentials_env` prefix (e.g. `"MITRE_TAXII"`); the collector reads `MITRE_TAXII_USER` and `MITRE_TAXII_PASS`.
- `credentials_env` is optional; omit it for unauthenticated feeds.

### Submission to platform

- The collector uses a synchronous `httpx.Client` to call the platform's own REST API on localhost.
- Fetched STIX objects are submitted one TAXII page at a time via `POST /collectors/{id}/objects`.
- After each successful submission the cursor is updated via `PUT /collectors/{id}/cursor`.
- The collector respects the target bucket's mode: in append mode objects are inserted as-is; in merge mode the platform applies the configured merge strategy.

### Error handling

- Transient errors (network timeouts, HTTP 5xx) on either the remote TAXII server or the platform are retried with exponential back-off.
- HTTP 4xx responses from the remote TAXII server are treated as permanent failures (e.g. auth error) and are not retried.
- After `max_retries` consecutive failures in a poll cycle the collector logs an error and waits until the next scheduled poll.
- The cursor is only advanced after a batch is successfully accepted by the platform.

### Configuration (platform_config.yaml)

```yaml
collectors:
  - name: "mitre-attack"                                          # required; used for registration
    remote_url: "https://cti.mitre.org/taxii/"                   # required
    remote_collection_id: "95ecc380-afe9-11e4-9b6c-751b66dd541e" # required
    credentials_env: "MITRE_TAXII"                               # optional; omit for unauth feeds
    target_bucket: "Example collection"                          # required; must exist in buckets section
    poll_interval: 300                                           # seconds; default 300
    max_retries: 3                                               # default 3
    shutdown_timeout: 30                                         # seconds to wait on shutdown; default 30
```

## Platform API Requirements

The collector relies on the following platform endpoints (all to be implemented):

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/collectors/register` | Register collector; returns collector ID and cursor |
| POST | `/collectors/{id}/objects` | Submit a page of STIX objects |
| PUT | `/collectors/{id}/cursor` | Advance the cursor after a successful batch |

These endpoints require **no authentication** — they are internal-only and protected by network boundaries (localhost).

## Architecture Notes

- The collector thread is isolated from the asyncio event loop. It communicates with the platform exclusively via synchronous HTTP calls to localhost using `httpx.Client`.
- This design provides a clean extraction seam: to run the collector as a separate service, change the base URL from `localhost` to a remote address and add authentication — no collector logic changes required.
- Cursor state is stored in the database (via the platform API), so application restarts are safe.
- Multiple collectors can be registered and run concurrently as independent threads, each polling a different remote collection.
- The `taxii2-client` library is **not** used; TAXII 2.1 wire calls are made directly with synchronous `httpx.Client` using the appropriate `Accept: application/taxii+json;version=2.1` headers.

## Acceptance Criteria

- [ ] Collector registers with the platform on startup and receives a cursor; app crashes clearly on registration failure.
- [ ] Collector fetches only objects added after the cursor from the remote TAXII collection.
- [ ] All pages in a TAXII response are drained within a single poll cycle.
- [ ] Fetched objects are submitted to the platform and appear in the configured bucket.
- [ ] Cursor advances (to `X-TAXII-Date-Added-Last`) only after a batch is accepted by the platform.
- [ ] A collector restart does not re-ingest already-seen objects.
- [ ] Remote authentication credentials are read from env vars; never appear in YAML or code.
- [ ] Transient errors are retried with exponential back-off; permanent failures (4xx) are logged and not retried.
- [ ] Partial batch failures advance the cursor and log dropped objects; the poll continues.
- [ ] On shutdown the lifespan waits up to `shutdown_timeout` seconds for each thread; warns and abandons if exceeded.
- [ ] Unit tests cover cursor management, pagination loop, and retry logic using mocked `httpx` responses.
- [ ] Integration test verifies end-to-end flow using an in-process mock TAXII server as a pytest fixture.
