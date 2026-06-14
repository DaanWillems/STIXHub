# Bucket Config

## Overview

<<<<<<< HEAD
Buckets are declared in `platform_config.yaml` alongside collection config. At startup, the platform provisions any missing buckets and validates all collection references before serving requests. There is no runtime API for creating or modifying buckets — all changes require a config update and a restart.
=======
Buckets are defined in an external YAML file alongside collection config. At startup, the platform provisions any missing buckets and validates all collection references before serving requests. See [platform-config.md](platform-config.md) for how the YAML file is loaded and where it lives.
>>>>>>> main

## Goals

<<<<<<< HEAD
- Buckets and their storage mode are config-as-code — no runtime UI or API needed to manage them.
- The platform auto-provisions declared buckets on startup so operators do not have to run migrations manually.
- Unsafe mode transitions are caught at startup before any traffic is served.

## Non-Goals

- Runtime bucket creation or deletion via API.
- Per-collection storage modes — mode is a property of the bucket, not of any collection that writes to it.

## Config Schema

Buckets are declared in `platform_config.yaml` under a top-level `buckets` key.

```yaml
buckets:
  - name: raw-intel
    mode: append

  - name: processed-intel
    mode: merge
```

Rules:
- `name` must be unique across all declared buckets.
- `mode` is either `append` or `merge`.
  - **append** — multiple entities with the same STIX ID can coexist in the bucket.
  - **merge** — entities with the same STIX ID are deduplicated; the latest version wins.
- A bucket name referenced in a collection config must correspond to a declared bucket; unknown references are a non-fatal startup warning (see [Startup Behavior](#startup-behavior)).

## Startup Behavior

Bucket provisioning runs before collection validation. On each startup, the platform:

1. **Provisions buckets** — for each declared bucket, checks whether it already exists in the database:
   - If it does not exist, creates it with the configured mode.
   - If it already exists, validates the mode has not changed in a forbidden way (see [Mode Transition Rules](#mode-transition-rules)).
2. **Validates collections** — for each collection config entry, checks that the referenced bucket was successfully provisioned. A missing bucket causes that collection to be excluded from all API responses; startup continues.

## Mode Transition Rules

Changing a bucket's mode in config while the bucket already holds data carries risk. The platform enforces the following rules:

| Previous mode | Config mode | Result                  |
|---------------|-------------|-------------------------|
| (new)         | append      | Create bucket           |
| (new)         | merge       | Create bucket           |
| append        | append      | No-op                   |
| merge         | merge       | No-op                   |
| merge         | append      | Update mode             |
| append        | merge       | **Refuse to start**     |

The `append → merge` transition is forbidden because a bucket in append mode may already contain multiple entities with the same STIX ID. Switching to merge mode would leave the bucket in an inconsistent state. Operators who need to make this transition must migrate the data manually before changing the config.
=======
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
>>>>>>> main
