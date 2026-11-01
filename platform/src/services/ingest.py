import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from models.domain import Bucket, StixEntity, TaxiiStatusRef, TaxiiWriteStatusModel
from processors import stix as stix_processor
from repositories.bucket import BucketRepository


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
    now = datetime.now(UTC)

    successes: list[TaxiiStatusRef] = []
    failures: list[TaxiiStatusRef] = []
    entities_to_add: list[StixEntity] = []

    for raw_obj in raw_objects:
        obj_id: str = raw_obj.get("id", "unknown")
        try:
            _validate_stix_object(raw_obj)
            processed = stix_processor.process(raw_obj)
            entities_to_add.append(
                StixEntity(
                    id=0,
                    bucket_id=bucket.id,
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
            )
            successes.append(
                TaxiiStatusRef(
                    id=processed.platform_id,
                    version=raw_obj.get("modified"),
                )
            )
        except (NotImplementedError, ValueError, KeyError) as exc:
            failures.append(TaxiiStatusRef(id=obj_id, message=str(exc)))

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
