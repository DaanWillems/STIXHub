# Bucket Config

## Overview

Buckets are defined in code (config-as-code) alongside collection config. At startup, the platform provisions any missing buckets and validates all collection references before serving requests.

## BucketConfig

A new `BucketConfig` dataclass holds the bucket definition:

```python
@dataclass
class BucketConfig:
    name: str
    mode: BucketMode
```

`BucketMode` moves from `CollectionConfig` to `BucketConfig`, since mode is a property of how a bucket stores data, not of how a collection writes to it.

## CollectionConfig change

`CollectionConfig` loses the `mode` field:

```python
@dataclass
class CollectionConfig:
    taxii_collection: TaxiiCollectionModel
    bucket_name: str
```

## platform_config.py

A new `platform_config.py` module holds both config dicts. This is the single place to edit when wiring up new buckets and collections:

```python
BUCKET_CONFIGS: dict[str, BucketConfig] = {
    "raw-intel": BucketConfig(name="raw-intel", mode=BucketMode.append),
}

COLLECTION_CONFIGS: dict[str, CollectionConfig] = {
    "70a16fcf-...": CollectionConfig(
        taxii_collection=TaxiiCollectionModel(...),
        bucket_name="raw-intel",
    ),
}
```

`taxii2.py` imports `COLLECTION_CONFIGS` from here instead of defining it inline.

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
