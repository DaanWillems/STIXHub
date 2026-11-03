import pytest
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import async_sessionmaker

from models.domain import Bucket, ProcessingStatus, StixEntity
from database.repositories.bucket import DatabaseBucketRepository


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


async def test_save_bucket(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))

    assert bucket.id is not None
    assert bucket.name == "test-bucket"


async def test_get_bucket_by_id(bucket_repo: DatabaseBucketRepository):
    saved = await bucket_repo.save(Bucket(name="test-bucket"))

    retrieved = await bucket_repo.get(bucket_id=saved.id)

    assert retrieved.id == saved.id
    assert retrieved.name == "test-bucket"


async def test_get_bucket_by_name(bucket_repo: DatabaseBucketRepository):
    await bucket_repo.save(Bucket(name="test-bucket"))

    retrieved = await bucket_repo.get(bucket_name="test-bucket")

    assert retrieved.name == "test-bucket"


async def test_get_raises_when_neither_argument_given(
    bucket_repo: DatabaseBucketRepository,
):
    with pytest.raises(ValueError):
        await bucket_repo.get()


async def test_get_raises_when_bucket_not_found(bucket_repo: DatabaseBucketRepository):
    assert await bucket_repo.get(bucket_id=999999) is None


async def test_add_entities(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    entities = [
        make_entity(bucket.id, "indicator--1"),
        make_entity(bucket.id, "indicator--2"),
    ]

    await bucket_repo.add_entities(bucket.id, entities)

    stored = await bucket_repo.get_entities(bucket_id=bucket.id)
    assert len(stored) == 2
    assert {e.stix_id for e in stored} == {"indicator--1", "indicator--2"}


async def test_add_entities_stores_long_custom_type(
    bucket_repo: DatabaseBucketRepository,
) -> None:
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    assert bucket.id is not None
    entity = make_entity(bucket.id, "x-misp-galaxy-cluster--1")
    entity.type = "x-misp-galaxy-cluster-with-a-long-custom-name"

    await bucket_repo.add_entities(bucket.id, [entity])

    [stored] = await bucket_repo.get_entities(bucket_id=bucket.id)
    assert stored.type == entity.type


async def test_add_entities_raises_when_bucket_not_found(
    bucket_repo: DatabaseBucketRepository,
):
    with pytest.raises(ValueError):
        await bucket_repo.add_entities(999999, [make_entity(999999)])


async def test_update_entities(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(
        bucket.id,
        [
            make_entity(bucket.id, "indicator--1"),
            make_entity(bucket.id, "indicator--2"),
        ],
    )
    stored = await bucket_repo.get_entities(bucket_id=bucket.id)
    to_update = next(e for e in stored if e.stix_id == "indicator--1")
    untouched = next(e for e in stored if e.stix_id == "indicator--2")
    to_update.status = ProcessingStatus.processed
    to_update.type = "test"
    to_update.object = {"id": to_update.stix_id, "type": "test"}

    await bucket_repo.update_entities(bucket.id, [to_update])

    refreshed = await bucket_repo.get_entities(bucket_id=bucket.id)
    updated = next(e for e in refreshed if e.stix_id == "indicator--1")
    still_untouched = next(e for e in refreshed if e.stix_id == "indicator--2")
    assert updated.status == ProcessingStatus.processed
    assert updated.type == "test"
    assert updated.object == {"id": "indicator--1", "type": "test"}
    assert still_untouched.status == untouched.status
    assert still_untouched.type == untouched.type


async def test_update_entities_raises_when_bucket_not_found(
    bucket_repo: DatabaseBucketRepository,
):
    with pytest.raises(ValueError):
        await bucket_repo.update_entities(999999, [make_entity(999999)])


async def test_update_entities_ignores_unknown_entity_id(
    bucket_repo: DatabaseBucketRepository,
):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(bucket.id, [make_entity(bucket.id, "indicator--1")])
    stored = await bucket_repo.get_entities(bucket_id=bucket.id)

    unknown = make_entity(bucket.id, "indicator--missing")
    unknown.id = 999999
    await bucket_repo.update_entities(bucket.id, [unknown])

    refreshed = await bucket_repo.get_entities(bucket_id=bucket.id)
    assert len(refreshed) == 1
    assert refreshed[0].status == stored[0].status
    assert refreshed[0].type == stored[0].type


async def test_get_entities_by_bucket_name(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(bucket.id, [make_entity(bucket.id)])

    stored = await bucket_repo.get_entities(bucket_name="test-bucket")

    assert len(stored) == 1


async def test_get_entities_returns_empty_for_new_bucket(
    bucket_repo: DatabaseBucketRepository,
):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))

    stored = await bucket_repo.get_entities(bucket_id=bucket.id)

    assert stored == []


async def test_entities_default_to_pending_status(
    bucket_repo: DatabaseBucketRepository,
):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(bucket.id, [make_entity(bucket.id)])

    stored = await bucket_repo.get_entities(bucket_id=bucket.id)

    assert stored[0].status == ProcessingStatus.pending


async def test_delete_bucket(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(bucket.id, [make_entity(bucket.id)])

    await bucket_repo.delete(bucket.id)

    assert await bucket_repo.get(bucket_id=bucket.id) is None
    assert await bucket_repo.get_entities(bucket_id=bucket.id) == []


async def test_get_entities_with_limit(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    stored = await bucket_repo.get_entities(bucket_id=bucket.id, limit=3)

    assert len(stored) == 3


async def test_get_entities_with_offset(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    stored = await bucket_repo.get_entities(bucket_id=bucket.id, offset=3)

    assert len(stored) == 2


async def test_get_entities_with_limit_and_offset(
    bucket_repo: DatabaseBucketRepository,
):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    stored = await bucket_repo.get_entities(bucket_id=bucket.id, limit=2, offset=2)

    assert len(stored) == 2


async def test_acquire_entities_marks_as_processing(
    bucket_repo: DatabaseBucketRepository,
):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(
        bucket.id,
        [
            make_entity(bucket.id, "indicator--1"),
            make_entity(bucket.id, "indicator--2"),
        ],
    )

    acquired = await bucket_repo.acquire_entities(bucket.id, 2)

    assert len(acquired) == 2
    assert all(e.status == ProcessingStatus.processing for e in acquired)


async def test_acquire_entities_respects_limit(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    acquired = await bucket_repo.acquire_entities(bucket.id, 3)

    assert len(acquired) == 3


async def test_acquire_entities_skips_already_processing(
    bucket_repo: DatabaseBucketRepository,
):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(4)]
    )

    first_batch = await bucket_repo.acquire_entities(bucket.id, 2)
    second_batch = await bucket_repo.acquire_entities(bucket.id, 4)

    first_ids = {e.stix_id for e in first_batch}
    second_ids = {e.stix_id for e in second_batch}
    assert first_ids.isdisjoint(second_ids)
    assert len(second_batch) == 2


async def test_data_persists_between_sessions(engine) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        async with session.begin():
            bucket = await DatabaseBucketRepository(session).save(
                Bucket(name="cross-session-test")
            )
            bucket_id = bucket.id

    async with session_factory() as session:
        async with session.begin():
            repo = DatabaseBucketRepository(session)
            retrieved = await repo.get(bucket_id=bucket_id)
            assert retrieved.name == "cross-session-test"
            await repo.delete(bucket_id)


async def test_acquire_entities_returns_empty_when_none_pending(
    bucket_repo: DatabaseBucketRepository,
):
    bucket = await bucket_repo.save(Bucket(name="test-bucket"))
    await bucket_repo.add_entities(bucket.id, [make_entity(bucket.id)])
    await bucket_repo.acquire_entities(bucket.id, 1)

    acquired = await bucket_repo.acquire_entities(bucket.id, 1)

    assert acquired == []


async def test_resolve_stix_ids(bucket_repo: DatabaseBucketRepository):
    bucket = await bucket_repo.save(Bucket(name="resolve-bucket"))
    other = await bucket_repo.save(Bucket(name="resolve-other"))
    mine = make_entity(bucket.id, "malware--platform")
    mine.other_stix_ids = ["malware--a", "malware--b"]
    theirs = make_entity(other.id, "malware--elsewhere")
    theirs.other_stix_ids = ["malware--c"]
    await bucket_repo.add_entities(bucket.id, [mine])
    await bucket_repo.add_entities(other.id, [theirs])

    resolved = await bucket_repo.resolve_stix_ids(
        bucket.id, ["malware--a", "malware--c", "malware--unknown"]
    )

    assert resolved == {"malware--a": "malware--platform"}
    assert await bucket_repo.resolve_stix_ids(bucket.id, []) == {}


async def test_find_by_refs_scoped_to_buckets_and_type(
    bucket_repo: DatabaseBucketRepository,
):
    readable = await bucket_repo.save(Bucket(name="readable"))
    hidden = await bucket_repo.save(Bucket(name="hidden"))

    identity = make_entity(readable.id, "identity--platform")
    identity.type = "identity"
    identity.value = "ACME Corp"
    identity.other_stix_ids = ["identity--original"]
    hidden_identity = make_entity(hidden.id, "identity--hidden")
    hidden_identity.type = "identity"
    await bucket_repo.add_entities(readable.id, [identity, make_entity(readable.id)])
    await bucket_repo.add_entities(hidden.id, [hidden_identity])

    found = await bucket_repo.find_by_refs(
        [readable.id],
        "identity",
        ["identity--platform", "identity--original", "identity--hidden", "indicator--abc123"],
    )

    assert set(found) == {"identity--platform", "identity--original"}
    assert found["identity--original"].value == "ACME Corp"
    assert await bucket_repo.find_by_refs([readable.id], "identity", []) == {}


async def test_get_entities_after(bucket_repo: DatabaseBucketRepository) -> None:
    bucket = await bucket_repo.save(Bucket(name="after-bucket"))
    assert bucket.id is not None
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{n}") for n in range(3)]
    )
    first, *_ = await bucket_repo.get_entities(bucket_id=bucket.id)

    after = await bucket_repo.get_entities_after(bucket.id, first.id, limit=1)

    assert [e.stix_id for e in after] == ["indicator--1"]


async def test_find_latest_by_refs_prefers_newest(
    bucket_repo: DatabaseBucketRepository,
) -> None:
    bucket = await bucket_repo.save(Bucket(name="latest-bucket"))
    other = await bucket_repo.save(Bucket(name="latest-other"))
    assert bucket.id is not None and other.id is not None
    old, new, elsewhere = (
        make_entity(bucket.id, "ipv4-addr--platform"),
        make_entity(bucket.id, "ipv4-addr--platform"),
        make_entity(other.id, "ipv4-addr--platform"),
    )
    old.other_stix_ids, new.other_stix_ids = ["ipv4-addr--a"], ["ipv4-addr--b"]
    await bucket_repo.add_entities(bucket.id, [old, new])
    await bucket_repo.add_entities(other.id, [elsewhere])

    found = await bucket_repo.find_latest_by_refs(
        bucket.id, ["ipv4-addr--platform", "ipv4-addr--a", "ipv4-addr--x"]
    )

    assert found["ipv4-addr--platform"].other_stix_ids == ["ipv4-addr--b"]
    assert found["ipv4-addr--a"].other_stix_ids == ["ipv4-addr--a"]
    assert "ipv4-addr--x" not in found


async def test_worker_process_batch_writes_sink_and_marks_source(
    bucket_repo: DatabaseBucketRepository,
):
    from models.domain import PlatformConfig
    from worker.__main__ import Worker

    config = PlatformConfig.model_validate(
        {
            "buckets": [
                {"name": "raw", "mode": "append"},
                {"name": "refined", "mode": "append"},
            ],
            "collections": [],
            "roles": [
                {"name": "refiner", "can_read": ["raw"], "can_write": ["refined"]}
            ],
            "pipelines": [
                {
                    "name": "refine",
                    "role": "refiner",
                    "sources": ["raw"],
                    "sinks": ["refined"],
                    "steps": [{"action": "add", "field": "labels", "value": "refined"}],
                }
            ],
        }
    )
    worker = Worker(config)
    [pipeline] = worker.pipeline_manager.pipelines()
    raw = await bucket_repo.save(Bucket(name="raw"))
    refined = await bucket_repo.save(Bucket(name="refined"))
    entity = make_entity(raw.id)
    entity.object["labels"] = []
    await bucket_repo.add_entities(raw.id, [entity])

    assert await worker.process_batch(pipeline, raw, bucket_repo) == 1

    [sink_entity] = await bucket_repo.get_entities(bucket_id=refined.id)
    assert sink_entity.status == ProcessingStatus.pending
    assert sink_entity.creator == "test"
    assert sink_entity.pipeline == "refine"
    assert sink_entity.object["labels"] == ["refined"]
    [source_entity] = await bucket_repo.get_entities(bucket_id=raw.id)
    assert source_entity.status == ProcessingStatus.processed
    assert source_entity.pipeline is None
    assert source_entity.object["labels"] == []
