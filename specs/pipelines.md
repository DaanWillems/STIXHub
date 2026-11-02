# Pipelines: roles and ref resolution

## Overview

A pipeline reads entities from its `sources` buckets, runs its `steps` (conditional add /
replace / delete actions on fields) and writes the result to its `sinks` buckets.

Buckets are **views** on intel. The same feed can land in a bucket that combines everything up
to TLP:RED and in another that only holds TLP:GREEN and below. Pipelines must keep those views
separate: intel that a reader of a sink may not see must never flow into it, and must never
influence how the pipeline treats the objects it does write there.

To enforce that, every pipeline **runs as a role** from the existing role system (see
[users-rbac.md](users-rbac.md)). It sees exactly what a user holding that role would see.
The operator decides how much: a broad role gives the pipeline a complete view, and a narrow
role gives it a partial one.

## Config

```yaml
roles:
  - name: refiner
    can_read:
      - raw-intel
    can_write:
      - refined-intel

pipelines:
  - name: tag-acme
    role: refiner            # required
    sources:
      - raw-intel
    sinks:
      - refined-intel
    steps:
      - action: add
        field: labels
        value: acme
        condition:
          field: created_by_ref
          type: eq
          value: ACME Corp   # a readable value, not identity--…
```

## Startup validation

When the worker loads its pipelines (`PipelineManager`), any of the following is a fatal
`PipelineValidationError`:

| Condition                                  | Error                                             |
|--------------------------------------------|---------------------------------------------------|
| `role` is not declared under `roles`       | `references unknown role '<role>'`                |
| A `sources` bucket not in `role.can_read`  | `role '<role>' cannot read source bucket '<b>'`   |
| A `sinks` bucket not in `role.can_write`   | `role '<role>' cannot write sink bucket '<b>'`    |
| Pipelines form a cycle (see below)         | `Pipelines form a cycle: a -> b -> a`             |

Effective permissions are computed by `permissions.get_effective_permissions`, the same
function the TAXII routes use for users.

## Writing to sinks

The worker (`worker/__main__.py`, `Worker.process_batch`) handles one batch per pipeline and
source bucket:

1. Acquire up to 10 `pending` entities from the source (`SELECT … FOR UPDATE SKIP LOCKED`,
   so several workers can share a bucket) and mark them `processing`.
2. Resolve every sink bucket. If one doesn't exist, the batch fails before anything is
   written (`SinkNotFoundError`).
3. Run the pipeline on copies of the entities (see the ref ↔ value resolution section below).
4. **Append** each result to **every** sink as a new entity, with status `pending`, creator
   `pipeline:<name>`, and `platform_created` / `platform_modified` set to now. `stix_id`,
   `other_stix_ids`, `type` and `value` carry over from the source entity. Each sink gets
   its own copy of the object.
5. Mark the source entities `processed`. Their objects are not modified.

All of this runs in **one database transaction**. If any step fails, nothing is written and
the source entities go back to `pending`.

Sinks are written in append mode **regardless of the bucket's mode**. Deduplication for
merge-mode sinks ("latest version wins", see [bucket-config.md](bucket-config.md)) is not
implemented yet.

### Chaining and cycles

Because sink entities start as `pending`, a pipeline that reads a bucket picks up what
another pipeline wrote there, so pipelines can be chained (`raw → refined → shared`). For the
same reason, the pipeline graph must be acyclic. Each pipeline adds an edge from each of its
sources to each of its sinks, and any cycle is a fatal startup error, including a pipeline
whose sink is one of its own sources:

```
PipelineValidationError: Pipelines form a cycle: raw -> refined -> shared -> raw
```

## Ref ↔ value resolution

Pipeline conditions and steps address `created_by_ref` and `object_marking_refs` by **readable
value** instead of STIX ID. Only these two fields are translated, because they are the only ref
fields a pipeline can act on. All other refs (`object_refs`, `source_ref`, `observable_refs`, …)
stay plain IDs.

| Field                 | Readable value                                              |
|-----------------------|-------------------------------------------------------------|
| `created_by_ref`      | The identity's `name`, e.g. `ACME Corp`                     |
| `object_marking_refs` | `tlp:clear`, `tlp:white`, `tlp:green`, `tlp:amber`, `tlp:amber+strict`, `tlp:red`, `statement:<text>`, or the marking's `name` |

The executor (`pipeline/refs.py`, `RefResolver`) works in three stages:

1. **Ref → value, before the steps run.** For each batch, all author and marking refs are
   looked up in one query per type with `BucketRepository.find_by_refs`. The lookup only
   covers the buckets the pipeline's **role can read**, not just its sources. A ref matches
   an entity's platform `stix_id` or any of its `other_stix_ids`, so original feed IDs
   resolve too. TLP markings resolve from a built-in registry of the well-known TLP 1.0 and
   2.0 IDs. They are public spec constants, not intel, and a TLP condition must never fail
   silently just because the marking object wasn't ingested.
2. **Steps run on a deep copy.** The source entity is never modified.
3. **Value → ref, afterwards.**
   - A value that was present on input gets its **original ref** back, byte for byte.
   - A new value gets its **deterministic platform ID**: `identity--uuid5({"value": name})`,
     or the TLP 2.0 ID for `tlp:*`. No lookup is needed and no object is created. If that
     identity or marking later flows into the sink, it arrives with the same ID and the ref
     resolves.

### Unknown refs

If a ref can't be resolved (not a TLP ID and not in a readable bucket), the **raw ID is kept**
and a warning is logged. Conditions on the readable value won't match, but conditions on the
raw ID still do, and the output keeps the original ref. Any value that already looks like a
STIX ID (`<type>--<uuid>`) passes through unchanged on the way back.

The same applies when an author exists only in a bucket the role can't read. That's
intentional: to the pipeline, that intel doesn't exist.

### Dangling refs in lower-TLP sinks

A TLP:GREEN object can be authored by an identity that is itself TLP:RED. A GREEN-only
pipeline filters the identity out, so the GREEN sink holds an object whose `created_by_ref`
points to an identity that isn't there. STIX allows unresolved refs, and this is the correct
result: the alternative leaks RED intel into GREEN.

## Marking definitions

`marking-definition` objects can be ingested like any other object (`processors/stix.py`):

- **TLP 1.0** (`definition_type: tlp`) and **TLP 2.0** (the
  `extension-definition--60a3c5c5-…` property extension) keep their well-known spec IDs as
  the platform ID, so consumers still recognize them.
- **Statement** markings and markings identified only by `name` get a deterministic uuid5 ID
  derived from their value.

## Not yet implemented

- Merge-mode sinks are not deduplicated; every result is appended.
- `Worker.run` processes one batch per source and exits; there is no polling loop yet.
- There is no action to drop an entity, so TLP gating ("skip anything marked tlp:red") can't be
  expressed yet.
- Ingest does not rewrite `created_by_ref` / `object_marking_refs` to platform IDs. The
  `other_stix_ids` lookup covers this during resolution.
