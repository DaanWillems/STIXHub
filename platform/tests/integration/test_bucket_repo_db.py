import pytest
from datetime import datetime, timezone
from sqlalchemy.exc import NoResultFound

from models.domain import Bucket, ProcessingStatus, StixEntity
from repositories.bucket import DatabaseBucketRepository


def make_entity(bucket_id: int, stix_id: str = "indicator--abc123") -> StixEntity:
    now = datetime.now(timezone.utc)
    return StixEntity(
        id=0,
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


async def test_save_bucket(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))

    assert bucket.id is not None
    assert bucket.name == "test-bucket"


async def test_get_bucket_by_id(repo: DatabaseBucketRepository):
    saved = await repo.save(Bucket(name="test-bucket"))

    retrieved = await repo.get(bucket_id=saved.id)

    assert retrieved.id == saved.id
    assert retrieved.name == "test-bucket"


async def test_get_bucket_by_name(repo: DatabaseBucketRepository):
    await repo.save(Bucket(name="test-bucket"))

    retrieved = await repo.get(bucket_name="test-bucket")

    assert retrieved.name == "test-bucket"


async def test_get_raises_when_neither_argument_given(repo: DatabaseBucketRepository):
    with pytest.raises(ValueError):
        await repo.get()


async def test_get_raises_when_bucket_not_found(repo: DatabaseBucketRepository):
    with pytest.raises(NoResultFound):
        await repo.get(bucket_id=999999)


async def test_add_entities(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    entities = [
        make_entity(bucket.id, "indicator--1"),
        make_entity(bucket.id, "indicator--2"),
    ]

    await repo.add_entities(bucket.id, entities)

    stored = await repo.get_entities(bucket_id=bucket.id)
    assert len(stored) == 2
    assert {e.stix_id for e in stored} == {"indicator--1", "indicator--2"}


async def test_add_entities_raises_when_bucket_not_found(
    repo: DatabaseBucketRepository,
):
    with pytest.raises(ValueError):
        await repo.add_entities(999999, [make_entity(999999)])


async def test_get_entities_by_bucket_name(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(bucket.id, [make_entity(bucket.id)])

    stored = await repo.get_entities(bucket_name="test-bucket")

    assert len(stored) == 1


async def test_get_entities_returns_empty_for_new_bucket(
    repo: DatabaseBucketRepository,
):
    bucket = await repo.save(Bucket(name="test-bucket"))

    stored = await repo.get_entities(bucket_id=bucket.id)

    assert stored == []


async def test_entities_default_to_pending_status(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(bucket.id, [make_entity(bucket.id)])

    stored = await repo.get_entities(bucket_id=bucket.id)

    assert stored[0].status == ProcessingStatus.pending


async def test_delete_bucket(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(bucket.id, [make_entity(bucket.id)])

    await repo.delete(bucket.id)

    with pytest.raises(NoResultFound):
        await repo.get(bucket_id=bucket.id)
    assert await repo.get_entities(bucket_id=bucket.id) == []


async def test_get_entities_with_limit(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    stored = await repo.get_entities(bucket_id=bucket.id, limit=3)

    assert len(stored) == 3


async def test_get_entities_with_offset(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    stored = await repo.get_entities(bucket_id=bucket.id, offset=3)

    assert len(stored) == 2


async def test_get_entities_with_limit_and_offset(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    stored = await repo.get_entities(bucket_id=bucket.id, limit=2, offset=2)

    assert len(stored) == 2


async def test_acquire_entities_marks_as_processing(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(
        bucket.id,
        [
            make_entity(bucket.id, "indicator--1"),
            make_entity(bucket.id, "indicator--2"),
        ],
    )

    acquired = await repo.acquire_entities(bucket.id, 2)

    assert len(acquired) == 2
    assert all(e.status == ProcessingStatus.processing for e in acquired)


async def test_acquire_entities_respects_limit(repo: DatabaseBucketRepository):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    acquired = await repo.acquire_entities(bucket.id, 3)

    assert len(acquired) == 3


async def test_acquire_entities_skips_already_processing(
    repo: DatabaseBucketRepository,
):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(4)]
    )

    first_batch = await repo.acquire_entities(bucket.id, 2)
    second_batch = await repo.acquire_entities(bucket.id, 4)

    first_ids = {e.stix_id for e in first_batch}
    second_ids = {e.stix_id for e in second_batch}
    assert first_ids.isdisjoint(second_ids)
    assert len(second_batch) == 2


async def test_acquire_entities_returns_empty_when_none_pending(
    repo: DatabaseBucketRepository,
):
    bucket = await repo.save(Bucket(name="test-bucket"))
    await repo.add_entities(bucket.id, [make_entity(bucket.id)])
    await repo.acquire_entities(bucket.id, 1)

    acquired = await repo.acquire_entities(bucket.id, 1)

    assert acquired == []
