# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands run from the `platform/` directory.

**Setup:**
```bash
cd platform
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

**Run the app:**
```bash
cd platform/src
uvicorn src.__main__:app --reload
```

**Run tests:**
```bash
cd platform
pytest
# Single test file:
pytest tests/unit/test_bucket_repo_memory.py
# With coverage:
pytest --cov=src
```

**Lint / type check:**
```bash
cd platform
ruff check src/
mypy src/
```

**Start dependencies (PostgreSQL):**
```bash
docker compose up -d
```

**Environment variables** (prefixed `stix_hub_`, can be set in `platform/src/.env`):
- `STIX_HUB_DATABASE_HOST`, `STIX_HUB_DATABASE_PORT`, `STIX_HUB_DATABASE_NAME`
- `STIX_HUB_DATABASE_USER`, `STIX_HUB_DATABASE_PASS`
- `STIX_HUB_DATABASE_USE_NULLPOOL` (default: false) — use NullPool; enabled in tests
- `STIX_HUB_DATABASE_ENGINE_ECHO` (default: true)

## Architecture

The application lives entirely in `platform/src/`. It is a **FastAPI + async SQLAlchemy** service backed by PostgreSQL (asyncpg driver).

### Core concepts

- **Buckets** — logical storage containers for STIX entities. Entities in a bucket can coexist with the same STIX ID (append mode) or be merged (merge mode).
- **STIX Entities** — raw STIX 2.1 objects stored as JSONB in PostgreSQL (`stix_entity_model` table), each associated with a bucket.
- **TAXII 2.1 Collections** — HTTP endpoints that expose bucket data following the TAXII 2.1 spec. A collection can be read-only or write-only (not both).
- **Collectors** — external processes that register with the platform and push STIX objects into buckets via REST.
- **Pipelines** — workers that poll buckets for new entities, apply filters/transforms, and write results to another bucket.

### Layer structure

```
platform/src/
  __main__.py       # FastAPI app factory, lifespan (create_tables / dispose)
  config.py         # pydantic-settings Settings class (env prefix: stix_hub_)
  database.py       # async SQLAlchemy engine + session factory; singleton `db`
  models/
    data.py         # SQLAlchemy ORM models (BucketModel, StixEntityModel) + StixType enum
    domain.py       # Pydantic/dataclass domain models + TAXII response models
  repositories/
    bucket.py       # BucketRepository ABC + InMemoryBucketRepository (for tests)
  routes/
    taxii2.py       # TAXII 2.1 router mounted at /taxii2
```

### Key design notes

- `pytest.ini_options` sets `pythonpath = "."` (relative to `platform/`) and `asyncio_mode = "auto"`, so async test functions work without decorators.
- Tests use `InMemoryBucketRepository` rather than a real DB; `DATABASE_USE_NULLPOOL=true` is used when a real DB connection is needed in tests to avoid connection pool issues.
- STIX IDs are intended to be generated deterministically by the platform on ingestion (the original ID is preserved in a separate field). This is required for the pipeline deduplication/locking mechanism.
- mypy is configured with `strict = true`; keep all new code fully typed.
