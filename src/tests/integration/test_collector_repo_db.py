from sqlalchemy.ext.asyncio import AsyncSession

from models.domain import Bucket
from database.repositories.bucket import DatabaseBucketRepository
from database.repositories.collector import DatabaseCollectorRepository


async def test_register_get_and_update_cursor(session: AsyncSession) -> None:
    bucket = await DatabaseBucketRepository(session).save(Bucket(name="feed-bucket"))
    assert bucket.id is not None
    repo = DatabaseCollectorRepository(session)

    collector = await repo.register("feed", bucket_id=bucket.id)
    assert collector.cursor is None

    await repo.update_cursor(collector.id, "2026-01-01T00:00:00Z")
    again = await repo.register("feed", bucket_id=bucket.id)

    assert again.id == collector.id
    assert again.cursor == "2026-01-01T00:00:00Z"
    fetched = await repo.get(collector.id)
    assert fetched is not None and fetched.cursor == "2026-01-01T00:00:00Z"


async def test_update_cursor_unknown_collector_returns_none(
    session: AsyncSession,
) -> None:
    repo = DatabaseCollectorRepository(session)
    assert await repo.update_cursor(999_999, "2026-01-01T00:00:00Z") is None
