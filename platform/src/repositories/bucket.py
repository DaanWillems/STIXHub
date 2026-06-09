from abc import ABC, abstractmethod
import uuid

from models.domain import Bucket, StixEntity


class BucketRepository(ABC):
    @abstractmethod
    async def save(self, bucket_in: Bucket) -> Bucket: ...

    @abstractmethod
    async def get(self, bucket_id: int = None, bucket_name: str = None) -> Bucket: ...

    @abstractmethod
    async def get_entities(self, bucket_id: int = None, bucket_name: str = None) -> list[StixEntity]: ...

    @abstractmethod
    async def add_entities(self, bucket_id: int, entities_in: list[StixEntity]) -> Bucket: ...

    @abstractmethod
    async def delete(self, bucket_id: int) -> None: ...


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
            for k, v in self._store.items():
                if v[0].name == bucket_name:
                    return v[0]
        else:
            raise Exception("Cannot filter on both id and name at the same time")
        return None
    
    async def get_entities(self, bucket_id: int = None, bucket_name: str = None) -> list[StixEntity]:
        if bucket_name is None and bucket_id is not None:
            return self._store[bucket_id][1]
        if bucket_name is not None and bucket_id is None:
            for k, v in self._store.items():
                if v[0].name == bucket_name:
                    return v[1]
        else:
            raise Exception("Cannot filter on both id and name at the same time")
        return None
    
    async def add_entities(self, bucket_id: int, entities_in: list[StixEntity]) -> Bucket:
        self._store[bucket_id][1] = self._store[bucket_id][1] + entities_in
        return self._store[bucket_id]
    
    async def delete(self, bucket_id: int) -> None:
        del self._store[bucket_id]

