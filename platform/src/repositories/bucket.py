from abc import ABC, abstractmethod

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.data import BucketModel, StixEntityModel
from models.domain import Bucket, ProcessingStatus, StixEntity


def _bucket_from_model(model: BucketModel) -> Bucket:
    return Bucket(id=model.id, name=model.name)


def _entity_from_model(model: StixEntityModel) -> StixEntity:
    return StixEntity(
        id=model.id,
        bucket_id=model.bucket_id,
        stix_id=model.stix_id,
        type=model.type,
        spec_version=model.spec_version,
        creator=model.creator,
        value=model.value,
        platform_modified=model.platform_modified,
        platform_created=model.platform_created,
        object=model.object,
        status=ProcessingStatus(model.status),
    )


class BucketRepository(ABC):
    @abstractmethod
    async def save(self, bucket_in: Bucket) -> Bucket: ...

    @abstractmethod
    async def get(self, bucket_id: int = None, bucket_name: str = None) -> Bucket: ...

    @abstractmethod
    async def get_entities(
        self, bucket_id: int = None, bucket_name: str = None
    ) -> list[StixEntity]: ...

    @abstractmethod
    async def add_entities(
        self, bucket_id: int, entities_in: list[StixEntity]
    ) -> Bucket: ...

    @abstractmethod
    async def delete(self, bucket_id: int) -> None: ...

    @abstractmethod
    async def acquire_entities(self, bucket_id: int, n: int) -> list[StixEntity]: ...


class InMemoryBucketRepository(BucketRepository):
    def __init__(self) -> None:
        self._index_counter: int = 0
        self._store: dict[int, tuple[Bucket, list[StixEntity]]] = {}

    async def save(self, bucket_in: Bucket) -> Bucket:
        bucket_in.id = self._index_counter
        self._index_counter += 1
        self._store[bucket_in.id] = (bucket_in, [])
        return bucket_in

    async def get(self, bucket_id: int = None, bucket_name: str = None) -> Bucket:
        if bucket_name is None and bucket_id is not None:
            return self._store[bucket_id][0]
        if bucket_name is not None and bucket_id is None:
            for _, v in self._store.items():
                if v[0].name == bucket_name:
                    return v[0]
        else:
            raise Exception("Cannot filter on both id and name at the same time")
        return None

    async def get_entities(
        self, bucket_id: int = None, bucket_name: str = None
    ) -> list[StixEntity]:
        if bucket_name is None and bucket_id is not None:
            return self._store[bucket_id][1]
        if bucket_name is not None and bucket_id is None:
            for _, v in self._store.items():
                if v[0].name == bucket_name:
                    return v[1]
        else:
            raise Exception("Cannot filter on both id and name at the same time")
        return None

    async def add_entities(
        self, bucket_id: int, entities_in: list[StixEntity]
    ) -> Bucket:
        self._store[bucket_id][1] = self._store[bucket_id][1] + entities_in
        return self._store[bucket_id]

    async def delete(self, bucket_id: int) -> None:
        del self._store[bucket_id]

    async def acquire_entities(self, bucket_id: int, n: int) -> list[StixEntity]:
        entities = self._store[bucket_id][1]
        acquired = []
        for entity in entities:
            if len(acquired) == n:
                break
            if entity.status == ProcessingStatus.pending:
                entity.status = ProcessingStatus.processing
                acquired.append(entity)
        return acquired


class DatabaseBucketRepository(BucketRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, bucket_in: Bucket) -> Bucket:
        model = BucketModel(name=bucket_in.name)
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _bucket_from_model(model)

    async def get(self, bucket_id: int = None, bucket_name: str = None) -> Bucket:
        if bucket_id is not None and bucket_name is None:
            result = await self._session.execute(
                select(BucketModel).where(BucketModel.id == bucket_id)
            )
            model = result.scalar_one()
            return _bucket_from_model(model)
        if bucket_name is not None and bucket_id is None:
            result = await self._session.execute(
                select(BucketModel).where(BucketModel.name == bucket_name)
            )
            model = result.scalar_one()
            return _bucket_from_model(model)
        raise ValueError("Provide exactly one of bucket_id or bucket_name")

    async def get_entities(
        self, bucket_id: int = None, bucket_name: str = None
    ) -> list[StixEntity]:
        if bucket_id is None and bucket_name is not None:
            bucket = await self.get(bucket_name=bucket_name)
            bucket_id = bucket.id
        elif bucket_id is None:
            raise ValueError("Provide exactly one of bucket_id or bucket_name")

        result = await self._session.execute(
            select(StixEntityModel).where(StixEntityModel.bucket_id == bucket_id)
        )
        return [_entity_from_model(row) for row in result.scalars().all()]

    async def add_entities(
        self, bucket_id: int, entities_in: list[StixEntity]
    ) -> Bucket:
        result = await self._session.execute(
            select(BucketModel).where(BucketModel.id == bucket_id)
        )
        if result.scalar_one_or_none() is None:
            raise ValueError(f"Bucket with id {bucket_id} does not exist")

        for entity in entities_in:
            model = StixEntityModel(
                bucket_id=bucket_id,
                stix_id=entity.stix_id,
                type=entity.type,
                spec_version=entity.spec_version,
                creator=entity.creator,
                value=entity.value,
                platform_modified=entity.platform_modified,
                object=entity.object,
            )
            self._session.add(model)
        await self._session.flush()
        return await self.get(bucket_id=bucket_id)

    async def delete(self, bucket_id: int) -> None:
        await self._session.execute(
            delete(StixEntityModel).where(StixEntityModel.bucket_id == bucket_id)
        )
        await self._session.execute(
            delete(BucketModel).where(BucketModel.id == bucket_id)
        )

    async def acquire_entities(self, bucket_id: int, n: int) -> list[StixEntity]:
        result = await self._session.execute(
            select(StixEntityModel)
            .where(
                StixEntityModel.bucket_id == bucket_id,
                StixEntityModel.status == ProcessingStatus.pending.value,
            )
            .limit(n)
            .with_for_update(skip_locked=True)
        )
        models = result.scalars().all()
        for model in models:
            model.status = ProcessingStatus.processing.value
        await self._session.flush()
        return [_entity_from_model(m) for m in models]
