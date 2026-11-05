from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter

from models.domain import (
    Bucket,
    BucketConfig,
    BucketMode,
    CollectorConfig,
    MispCollectorConfig,
)
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


def test_platform_config_misp_example_is_valid() -> None:
    """The commented MISP example in platform_config.yaml parses once enabled."""
    root = Path(__file__).resolve().parents[3]
    lines = (root / "platform_config.yaml").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(" # - name: "))
    block = []
    for line in lines[start:]:
        if not line.startswith(" #"):
            break
        block.append(line.replace(" # ", " ", 1))
    raw = yaml.safe_load("\n".join(block))
    configs = [TypeAdapter(CollectorConfig).validate_python(c) for c in raw]
    assert isinstance(configs[0], MispCollectorConfig)
    assert configs[0].name == "circl-misp"
