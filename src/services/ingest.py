import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from models.domain import Bucket, StixEntity, TaxiiStatusRef, TaxiiWriteStatusModel
from processors import stix as stix_processor
from database.repositories.bucket import BucketRepository


def _validate_stix_object(obj: dict[str, Any]) -> None:
    for field_name in ("id", "type", "spec_version"):
        if not obj.get(field_name):
            raise ValueError(f"Missing required field: '{field_name}'")


async def ingest_objects(
    repo: BucketRepository,
    bucket: Bucket,
    raw_objects: list[dict[str, Any]],
    creator: str,
) -> TaxiiWriteStatusModel:
    """Process raw STIX objects and store the valid ones in ``bucket``.

    Objects that fail validation or processing are reported as failures in the
    returned status; they do not abort the batch.
    """
    assert bucket.id is not None
    bucket_id: int = bucket.id
    now = datetime.now(UTC)

    results: dict[int, StixEntity | TaxiiStatusRef] = {}
    id_map: dict[str, str] = {}

    def _process(index: int) -> None:
        raw_obj = raw_objects[index]
        obj_id: str = raw_obj.get("id", "unknown")
        try:
            _validate_stix_object(raw_obj)
            processed = stix_processor.process(raw_obj, id_map)
        except (NotImplementedError, ValueError, KeyError) as exc:
            results[index] = TaxiiStatusRef(id=obj_id, message=str(exc))
            return
        id_map[obj_id] = processed.platform_id
        results[index] = StixEntity(
            id=0,
            bucket_id=bucket_id,
            stix_id=processed.platform_id,
            type=raw_obj["type"],
            spec_version=raw_obj["spec_version"],
            creator=creator,
            value=processed.value,
            platform_modified=now,
            platform_created=now,
            other_stix_ids=processed.other_stix_ids,
            object=processed.object,
        )

    # Some IDs derive from refs (a relationship's source and target, a
    # connection's addresses), so an object is processed after the objects in
    # this batch that it references. Refs to objects outside the batch may
    # point at objects already stored in the bucket under their original ID;
    # refs found nowhere are kept as-is.
    refs = {
        index: stix_processor.unresolved_refs(raw_obj) if "type" in raw_obj else []
        for index, raw_obj in enumerate(raw_objects)
    }
    batch_ids = {raw_obj.get("id") for raw_obj in raw_objects}
    external = {ref for ids in refs.values() for ref in ids if ref not in batch_ids}
    if external:
        id_map.update(await repo.resolve_stix_ids(bucket_id, sorted(external)))

    done: set[str] = set()
    pending = list(range(len(raw_objects)))
    while pending:
        ready = [
            index
            for index in pending
            if all(ref in done or ref not in batch_ids for ref in refs[index])
        ]
        # A reference cycle: process what is left with the refs known so far.
        for index in ready or pending:
            _process(index)
            done.add(raw_objects[index].get("id", ""))
        pending = [index for index in pending if index not in results]

    successes: list[TaxiiStatusRef] = []
    failures: list[TaxiiStatusRef] = []
    entities_to_add: list[StixEntity] = []
    for index in range(len(raw_objects)):
        result = results[index]
        if isinstance(result, StixEntity):
            entities_to_add.append(result)
            successes.append(
                TaxiiStatusRef(
                    id=result.stix_id,
                    version=raw_objects[index].get("modified"),
                )
            )
        else:
            failures.append(result)

    if entities_to_add:
        await repo.add_entities(bucket.id, entities_to_add)

    status_str: Literal["complete", "complete_with_errors"] = (
        "complete_with_errors" if failures else "complete"
    )

    return TaxiiWriteStatusModel(
        id=str(uuid.uuid4()),
        status=status_str,
        request_timestamp=now.isoformat(),
        total_count=len(raw_objects),
        success_count=len(successes),
        failure_count=len(failures),
        pending_count=0,
        successes=successes,
        failures=failures,
    )
