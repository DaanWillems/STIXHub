from datetime import datetime, timezone

from server.models.domain import Bucket, ProcessingStatus, StixEntity
from server.repositories.bucket import InMemoryBucketRepository


def make_entity(
    bucket_id: int, entity_id: int = 0, stix_id: str = "indicator--abc123"
) -> StixEntity:
    now = datetime.now(timezone.utc)
    return StixEntity(
        id=entity_id,
        bucket_id=bucket_id,
        stix_id=stix_id,
        type="indicator",
        spec_version="2.1",
        creator="test",
        value="test-value",
        platform_modified=now,
        platform_created=now,
        object={"id": stix_id, "type": "indicator"},
    )


async def test_get_bucket_repo_memory():
    repository = InMemoryBucketRepository()
    await repository.save(Bucket(name="test-bucket"))

    bucket = await repository.get(bucket_name="test-bucket")
    assert bucket.name == "test-bucket"

    bucket = await repository.get(bucket_id=0)
    assert bucket.name == "test-bucket"


async def test_update_entities_repo_memory():
    repository = InMemoryBucketRepository()
    bucket = await repository.save(Bucket(name="test-bucket"))
    entity = make_entity(bucket.id, entity_id=1)
    await repository.add_entities(bucket.id, [entity])

    updated = make_entity(bucket.id, entity_id=1)
    updated.status = ProcessingStatus.processed
    updated.type = "test"
    await repository.update_entities(bucket.id, [updated])

    stored = await repository.get_entities(bucket_id=bucket.id)
    assert len(stored) == 1
    assert stored[0].status == ProcessingStatus.processed
    assert stored[0].type == "test"


# async def test_get_bucket_entities_repo_memory():
#     repository = InMemoryBucketRepository()
#     bucket = await repository.save(Bucket(name="test-bucket"))
#     repository.add_entities(bucket_id=bucket.id, [StixEntity()])
