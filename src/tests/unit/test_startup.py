import pytest

from models.domain import Bucket, BucketConfig, BucketMode
from database.repositories.bucket import InMemoryBucketRepository
from routes.taxii2 import provision_buckets


async def test_provision_creates_missing_bucket() -> None:
    repo = InMemoryBucketRepository()
    configs = [BucketConfig(name="my-bucket", mode=BucketMode.append)]

    await provision_buckets(repo, configs)

    bucket = await repo.get(bucket_name="my-bucket")
    assert bucket.name == "my-bucket"
    assert bucket.mode == BucketMode.append


async def test_provision_is_idempotent() -> None:
    repo = InMemoryBucketRepository()
    configs = [BucketConfig(name="my-bucket", mode=BucketMode.append)]

    await provision_buckets(repo, configs)
    await provision_buckets(repo, configs)

    bucket = await repo.get(bucket_name="my-bucket")
    assert bucket is not None


async def test_provision_merge_to_append_updates_mode() -> None:
    repo = InMemoryBucketRepository()
    await repo.save(Bucket(name="my-bucket", mode=BucketMode.merge))
    configs = [BucketConfig(name="my-bucket", mode=BucketMode.append)]

    await provision_buckets(repo, configs)

    bucket = await repo.get(bucket_name="my-bucket")
    assert bucket.mode == BucketMode.append


async def test_provision_append_to_merge_raises() -> None:
    repo = InMemoryBucketRepository()
    await repo.save(Bucket(name="my-bucket", mode=BucketMode.append))
    configs = [BucketConfig(name="my-bucket", mode=BucketMode.merge)]

    with pytest.raises(RuntimeError, match="cannot change from append to merge"):
        await provision_buckets(repo, configs)


async def test_provision_same_mode_is_no_op() -> None:
    repo = InMemoryBucketRepository()
    await repo.save(Bucket(name="my-bucket", mode=BucketMode.merge))
    configs = [BucketConfig(name="my-bucket", mode=BucketMode.merge)]

    await provision_buckets(repo, configs)

    bucket = await repo.get(bucket_name="my-bucket")
    assert bucket.mode == BucketMode.merge


async def test_provision_multiple_buckets() -> None:
    repo = InMemoryBucketRepository()
    configs = [
        BucketConfig(name="bucket-a", mode=BucketMode.append),
        BucketConfig(name="bucket-b", mode=BucketMode.merge),
    ]

    await provision_buckets(repo, configs)

    a = await repo.get(bucket_name="bucket-a")
    b = await repo.get(bucket_name="bucket-b")
    assert a.mode == BucketMode.append
    assert b.mode == BucketMode.merge
