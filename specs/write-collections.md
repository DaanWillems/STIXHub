# Write Collections

## Overview

Write collections expose a TAXII 2.1 compliant `POST /root/collections/{id}/objects/` endpoint. Submitted STIX objects are validated, re-identified with a platform-generated deterministic ID, and written to a configured bucket in append mode.

## Collection Configuration

Collections are defined in an external YAML file, not stored in the database and not hardcoded in Python. See [platform-config.md](platform-config.md) for the file format and loading mechanism.

`CollectionConfig` is a pydantic `BaseModel` that holds TAXII fields plus the internal `bucket_name` routing field:

```python
class CollectionConfig(BaseModel):
    id: str
    title: str
    description: str
    can_read: bool
    can_write: bool
    bucket_name: str
```

`bucket_name` is platform-internal and never exposed to clients. `BucketMode` is a property of the bucket, not the collection — see [bucket-config.md](bucket-config.md). The route layer constructs a `TaxiiCollectionModel` from a `CollectionConfig` when building API responses.

## Startup Validation

Bucket provisioning runs before collection validation. See [bucket-config.md](bucket-config.md) for the full startup sequence. After buckets are provisioned, the platform validates collection config: for each `CollectionConfig`, check the referenced `bucket_name` exists in the database. For each missing bucket:
- Log an error
- Exclude that collection from all API responses
- Continue starting up (do not crash)

## Write Endpoint

**`POST /taxii2/root/collections/{collection_id}/objects/`**

Request body: a STIX 2.1 bundle (`application/stix+json;version=2.1`).

Response: `202 Accepted` with an inline synchronous TAXII status object. No async polling endpoint (`GET /status/{id}`) is implemented in Phase 1.

### Partial Success

Each object in the bundle is processed independently. Valid objects are written to the bucket. Invalid objects are reported as failures. The status response includes `success_count`, `failure_count`, and per-object success/failure references.

## Validation

Minimal validation is applied to each object:
- `id` is present and non-empty
- `type` is present and non-empty
- `spec_version` is present and non-empty

Full STIX 2.1 schema validation is out of scope for Phase 1.

## STIX Processors

Each STIX type has a processor responsible for:
1. Extracting the `value` — a type-specific searchable string (e.g. the IP address for `ipv4-addr`, the pattern for `indicator`)
2. Generating a deterministic platform ID via UUID v5

Processors are implemented for all SDOs and SCOs. Receiving an object whose type has no processor raises an unimplemented error; that object is reported as a failure.

### Deterministic ID Generation

Platform IDs are generated as UUID v5 using:
- **SCOs**: contributing properties as defined in the STIX 2.1 specification
- **SDOs**: the `value` field extracted by the processor (e.g. `pattern` for `indicator`, `name` for `malware`)

The STIX 2.1 namespace UUID (`00abedb4-aa42-466c-9c01-fed23315a9b7`) is used for all UUID v5 generation.

## Database Changes

### `StixEntityModel`

| Field | Change | Notes |
|---|---|---|
| `stix_id` | Existing — repurposed | Stores the platform-generated deterministic ID |
| `other_stix_ids` | New — JSONB array | Stores original STIX IDs from the source object |
| `value` | Existing | Populated by the STIX processor |
| `creator` | Existing | Set to the collection ID that wrote the object |
