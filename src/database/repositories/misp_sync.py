from abc import ABC, abstractmethod

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.domain import MispEventOrigin, MispEventSync
from models.models import MispEventSyncModel


class OwnershipError(ValueError):
    """An update tried to hand a MISP-owned event back to STIXHub."""


def _check_transfer(current: MispEventSync | None, update: MispEventSync) -> None:
    if (
        current is not None
        and current.origin is MispEventOrigin.misp
        and update.origin is MispEventOrigin.stixhub
    ):
        raise OwnershipError(
            f"MISP event {update.event_uuid} is owned by MISP; "
            "ownership never moves back to STIXHub"
        )


def _from_model(model: MispEventSyncModel) -> MispEventSync:
    return MispEventSync(
        instance=model.instance,
        event_uuid=model.event_uuid,
        origin=MispEventOrigin(model.origin),
        misp_timestamp=model.misp_timestamp,
        creator_org=model.creator_org,
    )


class MispSyncRepository(ABC):
    @abstractmethod
    async def get_many(
        self, instance: str, event_uuids: list[str]
    ) -> dict[str, MispEventSync]:
        """Records of ``instance`` for ``event_uuids``, keyed by event UUID."""

    @abstractmethod
    async def upsert(self, record: MispEventSync) -> MispEventSync:
        """Create or replace a record.

        Raises ``OwnershipError`` when it would move a MISP-owned event back
        to STIXHub.
        """


class InMemoryMispSyncRepository(MispSyncRepository):
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], MispEventSync] = {}

    async def get_many(
        self, instance: str, event_uuids: list[str]
    ) -> dict[str, MispEventSync]:
        return {
            uuid: self._store[(instance, uuid)]
            for uuid in event_uuids
            if (instance, uuid) in self._store
        }

    async def upsert(self, record: MispEventSync) -> MispEventSync:
        key = (record.instance, record.event_uuid)
        _check_transfer(self._store.get(key), record)
        self._store[key] = record
        return record


class DatabaseMispSyncRepository(MispSyncRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get_models(
        self, instance: str, event_uuids: list[str]
    ) -> dict[str, MispEventSyncModel]:
        if not event_uuids:
            return {}
        result = await self._session.execute(
            select(MispEventSyncModel).where(
                MispEventSyncModel.instance == instance,
                MispEventSyncModel.event_uuid.in_(event_uuids),
            )
        )
        return {model.event_uuid: model for model in result.scalars().all()}

    async def get_many(
        self, instance: str, event_uuids: list[str]
    ) -> dict[str, MispEventSync]:
        models = await self._get_models(instance, event_uuids)
        return {uuid: _from_model(model) for uuid, model in models.items()}

    async def upsert(self, record: MispEventSync) -> MispEventSync:
        # One statement, so connectors writing the same record at once can't
        # collide on the insert or slip past the ownership check.
        values = {
            "instance": record.instance,
            "event_uuid": record.event_uuid,
            "origin": record.origin.value,
            "misp_timestamp": record.misp_timestamp,
            "creator_org": record.creator_org,
        }
        insert = pg_insert(MispEventSyncModel).values(**values)
        moves_back_to_stixhub = (MispEventSyncModel.origin == MispEventOrigin.misp) & (
            insert.excluded.origin == MispEventOrigin.stixhub
        )
        statement = insert.on_conflict_do_update(
            index_elements=["instance", "event_uuid"],
            set_={**values, "updated_at": func.now()},
            where=~moves_back_to_stixhub,
        ).returning(MispEventSyncModel)
        # Refresh a copy of the row this session may have loaded before.
        result = await self._session.execute(
            statement, execution_options={"populate_existing": True}
        )
        model = result.scalar_one_or_none()
        if model is None:  # the update was refused by the where clause
            raise OwnershipError(
                f"MISP event {record.event_uuid} is owned by MISP; "
                "ownership never moves back to STIXHub"
            )
        return _from_model(model)
