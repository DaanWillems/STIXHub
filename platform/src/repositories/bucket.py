from abc import ABC, abstractmethod
import uuid

from models.domain import Bucket


class BucketRepository(ABC):
    @abstractmethod
    async def save(self, bucket_in: Bucket) -> Bucket: ...

    @abstractmethod
    async def get(self, bucket_id: int = None, bucket_name: str = None) -> Bucket: ...

    @abstractmethod
    async def delete(self, bucket_id: int) -> None: ...


class InMemoryTokenRepository(BucketRepository):
    def __init__(self) -> None:
        self._index_counter: int = 0
        self._store: dict[int, Bucket] = {}

    async def save(self, bucket_in: Bucket) -> Bucket:
        bucket_in.id = self._index_counter
        self._index_counter += 1
        self._store[bucket_in.id] = bucket_in

    async def get(self, bucket_id: int = None, bucket_name: str = None) -> Bucket:
        if bucket_name is None and bucket_id is not None:
            return self._store[bucket_id]
        if bucket_name is not None and bucket_id is None:
            for k, v in self._store.items():
                if v.name == bucket_name:
                    return v
        raise Exception("Cannot filter on both id and name at the same time")

    async def delete(self, bucket_id: int) -> None:
        del self._store[bucket_id]
