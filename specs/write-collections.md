# Write Collections

## Overview

Write collections expose a TAXII 2.1 compliant write endpoint. Submitted STIX bundles are validated, each object is re-identified with a platform-generated deterministic ID, and valid objects are written to the collection's configured bucket.

## Goals

- Callers can push STIX 2.1 bundles to a collection via a standard TAXII 2.1 write endpoint.
- Each ingested object gets a stable, deterministic platform ID derived from its content, independent of the source-assigned STIX ID.
- Partial success is supported — valid objects in a bundle are written even if others fail.
- Collections are wired to buckets via config, with no runtime API for managing the mapping.

## Non-Goals

- Async status polling — the write response is synchronous.
- Full STIX 2.1 schema validation — only minimal field presence is checked in Phase 1.
- Write endpoints for all bucket modes — write collections only target append-mode buckets.

## Collection Configuration

Collections are declared in `platform_config.yaml` and mapped to buckets by name. Each collection exposes one bucket; a bucket may back multiple collections.

```yaml
collections:
  - id: 70a16fcf-d221-4f00-b5b0-ea3b6f7c4ef5
    title: Raw Intel Feed
    description: Incoming threat intelligence from external sources
    bucket: raw-intel
    can_read: true
    can_write: true
```

Rules:
- `id` must be a valid UUID and unique across all declared collections.
- `bucket` must match a declared bucket name; see [bucket-config.md](bucket-config.md) for bucket provisioning.
- A collection whose referenced bucket is not found at startup is excluded from all API responses.

## Write Endpoint

**`POST /taxii2/root/collections/{collection_id}/objects/`**

Accepts a STIX 2.1 bundle (`application/stix+json;version=2.1`). Each object in the bundle is processed independently. The endpoint responds synchronously with `202 Accepted` and a TAXII status object summarising the result. No async polling endpoint is provided in Phase 1.

The status response includes a success count, failure count, and per-object success or failure references.

## Object Validation

Each submitted object is checked for minimal field presence before processing:
- `id` is present and non-empty.
- `type` is present and non-empty.
- `spec_version` is present and non-empty.

Objects that fail this check are recorded as failures in the status response. Full STIX 2.1 schema validation is out of scope for Phase 1.

## Deterministic IDs

Every valid object is assigned a platform-generated deterministic ID before being written to the bucket. This ID is derived from the object's content using UUID v5, ensuring the same logical object always maps to the same platform ID regardless of its source-assigned STIX ID.

The contributing properties used to derive the ID differ by object type:
- **SCOs** — contributing properties as defined in the STIX 2.1 specification.
- **SDOs** — a type-specific "value" field (e.g. the pattern for `indicator`, the name for `malware`).

The original source STIX ID is preserved alongside the platform ID and is never discarded.

Objects whose type is not yet supported are reported as failures.

## Error Cases

| Condition                              | Outcome                              |
|----------------------------------------|--------------------------------------|
| Collection not found                   | 404 Not Found                        |
| Invalid or missing content-type header | 415 Unsupported Media Type           |
| Object missing required fields         | Recorded as failure in status object |
| Unsupported STIX type                  | Recorded as failure in status object |
