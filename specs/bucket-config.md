# Bucket Config

## Overview

Buckets are defined in an external YAML file alongside collection config. At startup, the platform provisions any missing buckets and validates all collection references before serving requests. See [platform-config.md](platform-config.md) for how the YAML file is loaded and where it lives.

## BucketConfig

`BucketConfig` is a pydantic `BaseModel`:

```python
class BucketConfig(BaseModel):
    name: str
    mode: BucketMode
```

`BucketMode` is a property of how a bucket stores data, not of how a collection writes to it.

## CollectionConfig

`CollectionConfig` is a pydantic `BaseModel` with no `mode` field:

```python
class CollectionConfig(BaseModel):
    id: str
    title: str
    description: str
    can_read: bool
    can_write: bool
    bucket_name: str
```

Both models live in `models/domain.py` and are parsed from YAML via the `PlatformConfig` wrapper — see [platform-config.md](platform-config.md).

## Database change

`BucketModel` gains a `mode` field. It is set from config at startup and is never writable via API.

## Startup sequence

1. **Provision buckets** — for each `BucketConfig`:
   - If the bucket does not exist in the DB → create it with the configured mode.
   - If the bucket already exists → check for an invalid mode transition:
     - `append` → `merge` in config: **refuse to start** with a clear error message.
     - `merge` → `append` or no change: continue.
2. **Validate collections** — for each `CollectionConfig`, check the referenced `bucket_name` exists. Missing bucket → log error, exclude that collection from API responses, continue.

## Mode transition rules

| DB mode | Config mode | Result |
|---|---|---|
| (new) | append | Create bucket |
| (new) | merge | Create bucket |
| append | append | No-op |
| merge | merge | No-op |
| merge | append | Update mode |
| append | merge | **Refuse to start** |

The append→merge transition is forbidden because a bucket in append mode may contain multiple entities with the same STIX ID. Switching to merge mode would create an inconsistent state.
