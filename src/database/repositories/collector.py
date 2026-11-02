from abc import ABC, abstractmethod

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.models import CollectorModel
from models.domain import Collector


def _collector_from_model(model: CollectorModel) -> Collector:
    return Collector(
        id=model.id, name=model.name, bucket_id=model.bucket_id, cursor=model.cursor
    )


class CollectorRepository(ABC):
    @abstractmethod
    async def register(self, name: str, bucket_id: int) -> Collector:
        """Get or create the collector called ``name``.

        An existing collector keeps its cursor. If it is re-pointed at a
        different bucket the cursor is reset, since it described another bucket.
        """

    @abstractmethod
    async def get(self, collector_id: int) -> Collector | None: ...

    @abstractmethod
    async def update_cursor(self, collector_id: int, cursor: str) -> Collector | None:
        """Set the cursor; returns ``None`` if the collector does not exist."""


class InMemoryCollectorRepository(CollectorRepository):
    def __init__(self) -> None:
        self._next_id = 1
        self._store: dict[int, Collector] = {}

    async def register(self, name: str, bucket_id: int) -> Collector:
        for collector in self._store.values():
            if collector.name == name:
                if collector.bucket_id != bucket_id:
                    collector.bucket_id = bucket_id
                    collector.cursor = None
                return collector
        collector = Collector(id=self._next_id, name=name, bucket_id=bucket_id)
        self._store[collector.id] = collector
        self._next_id += 1
        return collector

    async def get(self, collector_id: int) -> Collector | None:
        return self._store.get(collector_id)

    async def update_cursor(self, collector_id: int, cursor: str) -> Collector | None:
        collector = self._store.get(collector_id)
        if collector is not None:
            collector.cursor = cursor
        return collector


class DatabaseCollectorRepository(CollectorRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, name: str, bucket_id: int) -> Collector:
        result = await self._session.execute(
            select(CollectorModel).where(CollectorModel.name == name)
        )
        model = result.scalar_one_or_none()
        if model is None:
            model = CollectorModel(name=name, bucket_id=bucket_id)
            self._session.add(model)
        elif model.bucket_id != bucket_id:
            model.bucket_id = bucket_id
            model.cursor = None
        await self._session.flush()
        return _collector_from_model(model)

    async def get(self, collector_id: int) -> Collector | None:
        model = await self._session.get(CollectorModel, collector_id)
        return None if model is None else _collector_from_model(model)

    async def update_cursor(self, collector_id: int, cursor: str) -> Collector | None:
        model = await self._session.get(CollectorModel, collector_id)
        if model is None:
            return None
        model.cursor = cursor
        await self._session.flush()
        return _collector_from_model(model)
