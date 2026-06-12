# Platform Config

## Overview

Buckets and collections are configured via an external YAML file, not hardcoded in Python. This makes the platform easy to deploy in Docker and Kubernetes without rebuilding the image.

## Config File Location

The path to the YAML file is read from the `STIX_HUB_PLATFORM_CONFIG` environment variable (consistent with the `stix_hub_` prefix used for all settings). It defaults to `platform_config.yaml` in the working directory.

The platform **crashes with a clear error** at startup if:
- The file does not exist at the resolved path
- The file is present but fails pydantic validation

## YAML Schema

```yaml
buckets:
  - name: raw-intel
    mode: append

collections:
  - id: 70a16fcf-8146-2da8-be66-6ca6fb7280af
    title: Example collection
    description: Raw intel feed
    can_read: true
    can_write: false
    bucket_name: raw-intel
```

`buckets` and `collections` are flat top-level lists. A collection references its bucket by `bucket_name`; there is no nesting. This keeps the door open for a collection to source from multiple buckets in the future.

## Pydantic Models

`BucketConfig` and `CollectionConfig` in `models/domain.py` are converted from dataclasses to pydantic `BaseModel`s. A `PlatformConfig` wrapper model holds both lists:

```python
class BucketConfig(BaseModel):
    name: str
    mode: BucketMode

class CollectionConfig(BaseModel):
    id: str
    title: str
    description: str
    can_read: bool
    can_write: bool
    bucket_name: str

class PlatformConfig(BaseModel):
    buckets: list[BucketConfig]
    collections: list[CollectionConfig]
```

`CollectionConfig` now holds the TAXII fields directly rather than wrapping a `TaxiiCollectionModel`. The route layer constructs a `TaxiiCollectionModel` from a `CollectionConfig` when building API responses.

## Settings Change

`config.py` gains one field:

```python
PLATFORM_CONFIG: str = "platform_config.yaml"
```

## Loading

The YAML file is loaded and validated once during the FastAPI lifespan startup, before bucket provisioning:

```python
raw = yaml.safe_load(path.read_text())
platform_config = PlatformConfig.model_validate(raw)
app.state.platform_config = platform_config
```

A pydantic `ValidationError` or a missing file both cause the process to exit with a descriptive message.

## Access Pattern

A FastAPI dependency provides `PlatformConfig` to routes:

```python
def get_platform_config(request: Request) -> PlatformConfig:
    return request.app.state.platform_config
```

Routes that previously imported `COLLECTION_CONFIGS` from `platform_config.py` use this dependency instead.

## File Removal

`platform_config.py` is deleted. An example file `platform_config.yaml` is committed to the repository root so developers can copy and edit it locally.

## Startup Sequence

1. Load and validate `platform_config.yaml` → crash on any error.
2. Provision buckets (see [bucket-config.md](bucket-config.md)).
3. Validate collection → bucket references (see [bucket-config.md](bucket-config.md)).
4. Start serving requests.
