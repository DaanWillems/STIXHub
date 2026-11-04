"""The platform's internal API, in-process, with in-memory repositories."""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from collector_auth import CollectorAuth
from collectors.platform_client import PlatformClient
from database.repositories.bucket import InMemoryBucketRepository
from database.repositories.collector import InMemoryCollectorRepository
from database.repositories.misp_sync import InMemoryMispSyncRepository
from dependencies import get_bucket_repo, get_collector_repo, get_misp_sync_repo
from models.domain import Bucket, CollectorConfig, StixEntity
from routes.collectors import collectors_router
from routes.misp_sync import misp_sync_router
from services.ingest import ingest_objects

BASE_URL = "http://platform"


def secret_of(collector: str) -> str:
    """The secret every test collector shares with the platform."""
    return f"secret-{collector}"


@dataclass
class Platform:
    app: FastAPI
    client: TestClient
    buckets: InMemoryBucketRepository
    sync: InMemoryMispSyncRepository
    collectors: list[CollectorConfig] = field(default_factory=list)

    def enroll(self, *configs: CollectorConfig) -> None:
        """Configure collectors, as their entries in platform_config.yaml would."""
        self.collectors.extend(configs)
        self.app.state.collector_auth = CollectorAuth.from_config(
            self.collectors, {c.name: secret_of(c.name) for c in self.collectors}
        )

    def api(self, collector: str) -> PlatformClient:
        """The internal API as ``collector`` sees it, holding its secret."""
        # TestClient is an httpx.Client that serves the app in-process.
        return PlatformClient(
            cast(httpx.Client, self.client), BASE_URL, secret_of(collector)
        )

    async def entities(self, bucket: str) -> list[StixEntity]:
        return await self.buckets.get_entities(bucket_name=bucket)

    async def ingest(
        self, bucket: str, objects: list[dict[str, Any]], creator: str
    ) -> None:
        """Write objects to a bucket as a feed would."""
        found = await self.buckets.get(bucket_name=bucket)
        assert found is not None
        await ingest_objects(self.buckets, found, objects, creator)

    async def copy(self, source: str, sink: str) -> None:
        """Append every entity of ``source`` to ``sink``, as a pipeline does."""
        found = await self.buckets.get(bucket_name=sink)
        assert found is not None and found.id is not None
        entities = await self.entities(source)
        await self.buckets.add_entities(
            found.id, [replace(e, id=0, bucket_id=found.id) for e in entities]
        )


async def make_platform(
    *buckets: str, collectors: Sequence[CollectorConfig] = ()
) -> Platform:
    bucket_repo = InMemoryBucketRepository()
    for name in buckets:
        await bucket_repo.save(Bucket(name=name))
    collector_repo = InMemoryCollectorRepository()
    sync_repo = InMemoryMispSyncRepository()
    app = FastAPI()
    app.include_router(collectors_router)
    app.include_router(misp_sync_router)
    app.dependency_overrides[get_bucket_repo] = lambda: bucket_repo
    app.dependency_overrides[get_collector_repo] = lambda: collector_repo
    app.dependency_overrides[get_misp_sync_repo] = lambda: sync_repo
    platform = Platform(app, TestClient(app, base_url=BASE_URL), bucket_repo, sync_repo)
    platform.enroll(*collectors)
    return platform
