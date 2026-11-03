"""A fake MISP instance and an in-process platform for MISP connector tests."""

import copy
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from fastapi import FastAPI
from fastapi.testclient import TestClient

from collectors.misp.client import MispEventRef, MispRequestError
from collectors.misp.connector import MispConnector
from collectors.platform_client import PlatformClient
from database.repositories.bucket import InMemoryBucketRepository
from database.repositories.collector import InMemoryCollectorRepository
from database.repositories.misp_sync import InMemoryMispSyncRepository
from dependencies import get_bucket_repo, get_collector_repo, get_misp_sync_repo
from models.domain import Bucket, MispConnectorConfig, StixEntity
from routes.collectors import collectors_router
from routes.misp_sync import misp_sync_router
from services.ingest import ingest_objects

EVENT_JSON = Path(__file__).parent / "fixtures" / "misp" / "event.json"
EVENT_UUID = "5e8d5c5a-0b1c-4b8e-9d2a-1a2b3c4d5e6f"
# 2026-09-21; every fake MISP timestamp is at or after this.
T0 = 1_790_000_000
NOW = datetime.fromtimestamp(T0, UTC)


def fixture_event(**changes: Any) -> dict[str, Any]:
    """The fixture event (attributes, objects, TLP tag), with ``changes`` applied."""
    event: dict[str, Any] = json.loads(EVENT_JSON.read_text())
    event.update(changes)
    return event


def small_event(uuid: str, info: str, timestamp: int, **changes: Any) -> dict[str, Any]:
    """An event with a single IP attribute."""
    return {
        "uuid": uuid,
        "info": info,
        "date": "2026-09-20",
        "timestamp": str(timestamp),
        "publish_timestamp": str(timestamp),
        "published": True,
        "Orgc": {"uuid": "11111111-2222-4333-8444-555555555555", "name": "ACME CERT"},
        "Attribute": [
            {
                "uuid": f"{uuid[:-12]}000000000001",
                "type": "ip-dst",
                "category": "Network activity",
                "value": "198.51.100.1",
                "to_ids": True,
                "timestamp": str(timestamp),
            }
        ],
        **changes,
    }


def all_uuids(event: dict[str, Any]) -> set[str]:
    """The UUIDs of an event's attributes and objects."""
    return {a["uuid"] for a in event.get("Attribute", [])} | {
        o["uuid"] for o in event.get("Object", [])
    }


@dataclass
class FakeMisp:
    """An in-memory MISP instance implementing ``MispApi``.

    Like MISP, it stamps every write with a new, increasing ``timestamp``.
    """

    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    clock: int = T0
    index_filters: list[dict[str, Any]] = field(default_factory=list)
    writes: list[tuple[str, str]] = field(default_factory=list)

    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def seed(self, event: dict[str, Any]) -> None:
        """Add an event as if a MISP user created it."""
        self.events[event["uuid"]] = copy.deepcopy(event)
        self.clock = max(self.clock, int(event["timestamp"]))

    def edit(self, uuid: str, **changes: Any) -> None:
        """Change an event as if a MISP user edited it."""
        self.events[uuid].update(changes, timestamp=str(self._tick()))

    def index_events(
        self,
        *,
        since: int,
        published_only: bool,
        tags: list[str],
        orgs: list[str],
        page: int,
        limit: int,
    ) -> list[MispEventRef]:
        self.index_filters.append(
            {
                "since": since,
                "published_only": published_only,
                "tags": tags,
                "orgs": orgs,
            }
        )
        rows = [
            MispEventRef(uuid, int(event["timestamp"]))
            for uuid, event in self.events.items()
            if int(event["timestamp"]) >= since
            and (event.get("published") or not published_only)
        ]
        return rows[(page - 1) * limit : page * limit]

    def get_event(self, uuid: str) -> dict[str, Any] | None:
        event = self.events.get(uuid)
        return copy.deepcopy(event) if event is not None else None

    def add_event(self, event: dict[str, Any]) -> int:
        if event["uuid"] in self.events:
            raise MispRequestError(403, "An event with this UUID already exists")
        self.writes.append(("add", event["uuid"]))
        timestamp = self._tick()
        self.events[event["uuid"]] = {
            **copy.deepcopy(event),
            "timestamp": str(timestamp),
        }
        return timestamp

    def update_event(self, uuid: str, event: dict[str, Any]) -> int:
        if uuid not in self.events:
            raise MispRequestError(404, "Invalid event")
        self.writes.append(("update", uuid))
        timestamp = self._tick()
        self.events[uuid] = {**copy.deepcopy(event), "timestamp": str(timestamp)}
        return timestamp

    def publish_event(self, uuid: str) -> None:
        self.writes.append(("publish", uuid))
        self.events[uuid]["published"] = True


@dataclass
class Platform:
    """The platform's internal API, in-process, with in-memory repositories."""

    client: TestClient
    buckets: InMemoryBucketRepository
    sync: InMemoryMispSyncRepository

    def api(self) -> PlatformClient:
        # TestClient is an httpx.Client that serves the app in-process.
        return PlatformClient(cast(httpx.Client, self.client), "http://platform")

    async def entities(self, bucket: str) -> list[StixEntity]:
        return await self.buckets.get_entities(bucket_name=bucket)

    async def ingest(
        self, bucket: str, objects: list[dict[str, Any]], creator: str
    ) -> None:
        """Write objects to a bucket as a feed (or a pipeline) would."""
        found = await self.buckets.get(bucket_name=bucket)
        assert found is not None
        await ingest_objects(self.buckets, found, objects, creator)

    async def copy(self, source: str, sink: str) -> None:
        """Append every entity of ``source`` to ``sink``, as a pipeline does."""
        found = await self.buckets.get(bucket_name=sink)
        assert found is not None and found.id is not None
        entities = await self.entities(source)
        await self.buckets.add_entities(
            found.id,
            [replace(e, id=0, bucket_id=found.id) for e in entities],
        )


async def make_platform(*buckets: str) -> Platform:
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
    return Platform(TestClient(app, base_url="http://platform"), bucket_repo, sync_repo)


def connector_config(**overrides: Any) -> MispConnectorConfig:
    values: dict[str, Any] = {
        "type": "misp",
        "name": "circl",
        "url": "https://misp.example.org",
        "credentials_env": "CIRCL",
        "pull": {"target_bucket": "raw"},
        "push": {"source_bucket": "refined"},
    }
    values.update(overrides)
    return MispConnectorConfig.model_validate(values)


def make_connector(
    config: MispConnectorConfig,
    misp: FakeMisp,
    platform: Platform,
    excluded_creators: frozenset[str] = frozenset(),
) -> MispConnector:
    connector = MispConnector(
        config,
        misp,
        platform.api(),
        excluded_creators=excluded_creators,
        stop_event=threading.Event(),
        backoff_base=0,
        clock=lambda: NOW,
    )
    connector.register()
    return connector
