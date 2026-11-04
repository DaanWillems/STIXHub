import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories.misp_sync import DatabaseMispSyncRepository, OwnershipError
from models.domain import MispEventOrigin, MispEventSync

EVENT = "5e8d5c5a-0b1c-4b8e-9d2a-1a2b3c4d5e6f"
INSTANCE = "https://misp.example.org"


async def test_upsert_creates_then_updates(session: AsyncSession) -> None:
    repo = DatabaseMispSyncRepository(session)

    await repo.upsert(MispEventSync(INSTANCE, EVENT, MispEventOrigin.stixhub))
    assert (await repo.get_many(INSTANCE, [EVENT]))[EVENT].misp_timestamp is None
    await repo.upsert(
        MispEventSync(INSTANCE, EVENT, MispEventOrigin.stixhub, 1790000000, "1")
    )

    assert await repo.get_many(INSTANCE, [EVENT, "unknown"]) == {
        EVENT: MispEventSync(INSTANCE, EVENT, MispEventOrigin.stixhub, 1790000000, "1")
    }
    assert await repo.get_many("https://other.example", [EVENT]) == {}


async def test_ownership_never_moves_back_to_stixhub(session: AsyncSession) -> None:
    repo = DatabaseMispSyncRepository(session)
    await repo.upsert(MispEventSync(INSTANCE, EVENT, MispEventOrigin.misp, 1))

    with pytest.raises(OwnershipError):
        await repo.upsert(MispEventSync(INSTANCE, EVENT, MispEventOrigin.stixhub, 2))
