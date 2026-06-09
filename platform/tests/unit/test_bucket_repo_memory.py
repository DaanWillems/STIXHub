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
