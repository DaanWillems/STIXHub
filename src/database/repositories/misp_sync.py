from abc import ABC, abstractmethod

from sqlalchemy import select
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
        models = await self._get_models(record.instance, [record.event_uuid])
        model = models.get(record.event_uuid)
        _check_transfer(None if model is None else _from_model(model), record)
        if model is None:
            model = MispEventSyncModel(
                instance=record.instance, event_uuid=record.event_uuid
            )
            self._session.add(model)
        model.origin = record.origin.value
        model.misp_timestamp = record.misp_timestamp
        model.creator_org = record.creator_org
        await self._session.flush()
        return _from_model(model)
