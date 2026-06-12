from collections.abc import AsyncGenerator

from config import settings
from database import db
from repositories.bucket import (
    BucketRepository,
    DatabaseBucketRepository,
    InMemoryBucketRepository,
)

_in_memory_repo = InMemoryBucketRepository()


async def get_bucket_repo() -> AsyncGenerator[BucketRepository, None]:
    if settings.BUCKET_REPO_BACKEND == "memory":
        yield _in_memory_repo
    else:
        async with db.get_session() as session:
            yield DatabaseBucketRepository(session)
