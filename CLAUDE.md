# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands run from the repository root unless noted.

**Setup:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

**Run the app** (`--mode server`, `worker` or `server-worker`):
```bash
PYTHONPATH=src python -m src --mode server
# or just the API with reload:
cd src && uvicorn server.__main__:app --reload
```

**Run tests** (integration tests need PostgreSQL; test env vars are set in `src/tests/conftest.py`):
```bash
pytest
# Single test file:
pytest src/tests/unit/test_bucket_repo_memory.py
# With coverage:
pytest --cov=src
```

**Lint / type check:**
```bash
ruff check src/
mypy src/
```

**Start dependencies (PostgreSQL):**
```bash
docker compose up -d
```

**Environment variables** (prefixed `stix_hub_`, can be set in a `.env` file in the working directory):
- `STIX_HUB_DATABASE_HOST`, `STIX_HUB_DATABASE_PORT`, `STIX_HUB_DATABASE_NAME`
- `STIX_HUB_DATABASE_USER`, `STIX_HUB_DATABASE_PASS`
- `STIX_HUB_DATABASE_USE_NULLPOOL` (default: false) — use NullPool; enabled in tests
- `STIX_HUB_DATABASE_ENGINE_ECHO` (default: true)
- `STIX_HUB_PLATFORM_CONFIG` (default: `platform_config.yaml`) — buckets, collections, roles, collectors, pipelines
- `STIX_HUB_ADMIN_API_KEY` — key for the `/users` admin endpoints

## Architecture

The application lives entirely in `src/`. It is a **FastAPI + async SQLAlchemy** service backed by PostgreSQL (asyncpg driver).

### Core concepts

- **Buckets** — logical storage containers for STIX entities. Entities in a bucket can coexist with the same STIX ID (append mode) or be merged (merge mode).
- **STIX Entities** — raw STIX 2.1 objects stored as JSONB in PostgreSQL (`stix_entity_model` table), each associated with a bucket.
- **TAXII 2.1 Collections** — HTTP endpoints that expose bucket data following the TAXII 2.1 spec. A collection can be read-only or write-only (not both).
- **Collectors** — external processes that register with the platform and push STIX objects into buckets via REST.
- **Pipelines** — workers that poll buckets for new entities, apply filters/transforms, and write results to another bucket. Each pipeline runs as a role and only resolves refs from buckets that role can read (see `specs/pipelines.md`).

### Layer structure

```
src/
  __main__.py       # entry point: --mode server | worker | server-worker
  config.py         # pydantic-settings Settings (env prefix: stix_hub_) + platform config loading
  permissions.py    # effective bucket permissions for a set of roles
  dependencies.py   # FastAPI dependencies (repos, current user, platform config)
  server/           # FastAPI app factory + lifespan (create tables, provision buckets, collectors)
  worker/           # pipeline worker loop
  database/
    database.py     # async SQLAlchemy engine + session factory; singleton `db`
    repositories/   # bucket, collector, user repos (DB + in-memory implementations)
  models/
    models.py       # SQLAlchemy ORM models + StixType enum
    domain.py       # Pydantic/dataclass domain + config models, TAXII response models
  processors/stix.py  # deterministic platform IDs/values per STIX type (incl. TLP markings)
  services/ingest.py  # shared ingestion path for TAXII writes and collectors
  collectors/       # TAXII collector runner
  pipeline/         # PipelineExecutor, PipelineManager, refs.py (ref <-> value resolution)
  merging/          # merge strategies (WIP)
  routes/           # taxii2, users, collectors routers
  tests/            # unit/ and integration/
```

### Key design notes

- `pytest.ini_options` in the root `pyproject.toml` sets `pythonpath = [".", "src"]` and `asyncio_mode = "auto"`, so async test functions work without decorators.
- Tests use `InMemoryBucketRepository` rather than a real DB; `DATABASE_USE_NULLPOOL=true` is used when a real DB connection is needed in tests to avoid connection pool issues.
- STIX IDs are intended to be generated deterministically by the platform on ingestion (the original ID is preserved in a separate field). This is required for the pipeline deduplication/locking mechanism.
- mypy is configured with `strict = true`; keep all new code fully typed.
