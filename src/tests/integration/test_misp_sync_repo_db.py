import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.repositories.misp_sync import DatabaseMispSyncRepository, OwnershipError
from models.domain import MispEventOrigin, MispEventSync

EVENT = "5e8d5c5a-0b1c-4b8e-9d2a-1a2b3c4d5e6f"


async def test_upsert_creates_then_updates(session: AsyncSession) -> None:
    repo = DatabaseMispSyncRepository(session)

    await repo.upsert(MispEventSync("circl", EVENT, MispEventOrigin.stixhub))
    await repo.upsert(
        MispEventSync("circl", EVENT, MispEventOrigin.stixhub, 1790000000)
    )

    assert await repo.get_many("circl", [EVENT, "unknown"]) == {
        EVENT: MispEventSync("circl", EVENT, MispEventOrigin.stixhub, 1790000000)
    }
    assert await repo.get_many("other", [EVENT]) == {}


async def test_ownership_never_moves_back_to_stixhub(session: AsyncSession) -> None:
    repo = DatabaseMispSyncRepository(session)
    await repo.upsert(MispEventSync("circl", EVENT, MispEventOrigin.misp, 1))

    with pytest.raises(OwnershipError):
        await repo.upsert(MispEventSync("circl", EVENT, MispEventOrigin.stixhub, 2))
