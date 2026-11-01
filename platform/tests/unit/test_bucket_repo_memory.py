from models.domain import Bucket
from repositories.bucket import InMemoryBucketRepository


async def test_get_bucket_repo_memory():
    repository = InMemoryBucketRepository()
    await repository.save(Bucket(name="test-bucket"))

    bucket = await repository.get(bucket_name="test-bucket")
    assert bucket.name == "test-bucket"

    bucket = await repository.get(bucket_id=0)
    assert bucket.name == "test-bucket"


# async def test_get_bucket_entities_repo_memory():
#     repository = InMemoryBucketRepository()
#     bucket = await repository.save(Bucket(name="test-bucket"))
#     repository.add_entities(bucket_id=bucket.id, [StixEntity()])


async def test_resolve_stix_ids_repo_memory() -> None:
    from datetime import UTC, datetime

    from models.domain import StixEntity

    repository = InMemoryBucketRepository()
    bucket = await repository.save(Bucket(name="b"))
    other = await repository.save(Bucket(name="other"))
    assert bucket.id is not None and other.id is not None
    now = datetime.now(UTC)

    def entity(bucket_id: int, stix_id: str, others: list[str]) -> StixEntity:
        return StixEntity(
            id=0,
            bucket_id=bucket_id,
            stix_id=stix_id,
            type="malware",
            spec_version="2.1",
            creator="test",
            value="v",
            platform_modified=now,
            platform_created=now,
            object={},
            other_stix_ids=others,
        )

    await repository.add_entities(
        bucket.id,
        [entity(bucket.id, "malware--platform", ["malware--a", "malware--b"])],
    )
    await repository.add_entities(
        other.id, [entity(other.id, "malware--elsewhere", ["malware--c"])]
    )

    resolved = await repository.resolve_stix_ids(
        bucket.id, ["malware--a", "malware--c", "malware--unknown"]
    )

    assert resolved == {"malware--a": "malware--platform"}
    assert await repository.resolve_stix_ids(bucket.id, []) == {}
