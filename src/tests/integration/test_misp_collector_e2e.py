"""End-to-end: mock MISP -> MispCollector -> collector API -> bucket.

Runs fully in-process (FastAPI TestClient for both the mock MISP server and
the platform, in-memory repositories), so no database is required.
"""

import copy
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from collectors.misp_client import MispClient
from collectors.misp_collector import MispCollector
from collectors.misp_cursor import MispCursor
from collectors.platform_client import PlatformClient
from database.repositories.bucket import InMemoryBucketRepository
from database.repositories.collector import InMemoryCollectorRepository
from dependencies import (
    get_bucket_repo,
    get_collector_repo,
    get_collector_secrets,
    get_platform_config,
)
from models.domain import Bucket, MispCollectorConfig, PlatformConfig
from routes.collectors import collectors_router

MISP_URL = "http://misp.test"
API_KEY = "misp-key"
BUCKET = "misp-intel"
COLLECTOR_NAME = "circl-misp"
PLATFORM_SECRET = "platform-secret"
PAGE_SIZE = 2
NOW = 1_704_200_000
FIXTURE = Path(__file__).parent.parent / "fixtures" / "misp_event.json"


# --- in-process mock MISP ----------------------------------------------------


@dataclass
class MockMisp:
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    listings: list[dict[str, Any]] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    hidden_from_view: set[str] = field(default_factory=set)

    def add_event(
        self, n: int, publish_timestamp: int, published: bool = True
    ) -> str:
        """Add (or replace) event ``n``, derived from the recorded fixture."""
        event: dict[str, Any] = copy.deepcopy(json.loads(FIXTURE.read_text())["Event"])
        uuid = f"00000000-0000-4000-8000-{n:012d}"
        event["uuid"] = uuid
        event["info"] = f"Event {n}"
        event["published"] = published
        event["publish_timestamp"] = str(publish_timestamp)
        self.events[uuid] = event
        return uuid

    def build_app(self) -> FastAPI:
        app = FastAPI()

        def check_auth(authorization: str | None) -> None:
            if authorization != API_KEY:
                raise HTTPException(status_code=403)

        @app.post("/events/restSearch")
        def rest_search(
            authorization: Annotated[str | None, Header()] = None,
            body: Annotated[dict[str, Any] | None, Body()] = None,
        ) -> dict[str, Any]:
            check_auth(authorization)
            assert body is not None
            self.listings.append(body)
            window = body["publish_timestamp"]
            if isinstance(window, list):
                low, high = int(window[0]), int(window[1])
            else:
                low, high = 0, int(str(window).removeprefix("<="))
            matching = sorted(
                (
                    e
                    for e in self.events.values()
                    if e["published"]
                    and low <= int(e["publish_timestamp"]) <= high
                ),
                key=lambda e: (int(e["publish_timestamp"]), e["uuid"]),
            )
            limit, page = int(body["limit"]), int(body["page"])
            chunk = matching[(page - 1) * limit : page * limit]
            return {
                "response": [
                    {
                        "Event": {
                            "uuid": e["uuid"],
                            "publish_timestamp": e["publish_timestamp"],
                        }
                    }
                    for e in chunk
                ]
            }

        @app.get("/events/view/{uuid}")
        def view(
            uuid: str, authorization: Annotated[str | None, Header()] = None
        ) -> dict[str, Any]:
            check_auth(authorization)
            self.views.append(uuid)
            if uuid not in self.events or uuid in self.hidden_from_view:
                raise HTTPException(status_code=404)
            return {"Event": self.events[uuid]}

        return app


@pytest.fixture
def misp_server() -> Iterator[tuple[MockMisp, TestClient]]:
    server = MockMisp()
    with TestClient(server.build_app(), base_url=MISP_URL) as client:
        yield server, client


# --- platform under test -----------------------------------------------------


@dataclass
class Platform:
    client: TestClient
    buckets: InMemoryBucketRepository


def _config() -> MispCollectorConfig:
    return MispCollectorConfig(
        name=COLLECTOR_NAME,
        type="misp",
        misp_url=MISP_URL,
        api_key_env="CIRCL_MISP_KEY",
        secret_env="CIRCL_MISP",
        target_bucket=BUCKET,
        page_size=PAGE_SIZE,
        poll_interval=3600,
        shutdown_timeout=5,
    )


@pytest.fixture
async def platform() -> Platform:
    buckets = InMemoryBucketRepository()
    await buckets.save(Bucket(name=BUCKET))
    collectors = InMemoryCollectorRepository()
    app = FastAPI()
    app.include_router(collectors_router)
    app.dependency_overrides[get_bucket_repo] = lambda: buckets
    app.dependency_overrides[get_collector_repo] = lambda: collectors
    app.dependency_overrides[get_platform_config] = lambda: PlatformConfig(
        buckets=[],
        collections=[],
        roles=[],
        pipelines=[],
        collectors=[_config()],
    )
    app.dependency_overrides[get_collector_secrets] = lambda: {
        "circl_misp": SecretStr(PLATFORM_SECRET)
    }
    return Platform(TestClient(app, base_url="http://platform"), buckets)


def _clock() -> datetime:
    return datetime.fromtimestamp(NOW, UTC)


def _bridge(app_client: TestClient) -> httpx.Client:
    """An ``httpx.Client`` that forwards to the in-process app.

    TestClient raises its own HTTP error type, which the collector (rightly)
    does not catch, so responses are re-created as plain ``httpx`` responses.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        response = app_client.request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(response.status_code, content=response.content)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _make_collector(
    misp_client: TestClient, platform: Platform, api_key: str = API_KEY
) -> MispCollector:
    return MispCollector(
        _config(),
        MispClient(_bridge(misp_client), MISP_URL, api_key),
        PlatformClient(_bridge(platform.client), "http://platform", PLATFORM_SECRET),
        backoff_base=0,
        clock=_clock,
    )


async def _entity_count(platform: Platform) -> int:
    return len(await platform.buckets.get_entities(bucket_name=BUCKET))


# --- tests -------------------------------------------------------------------


async def test_ingests_only_published_events_across_pages(
    misp_server: tuple[MockMisp, TestClient], platform: Platform
) -> None:
    server, client = misp_server
    published = [server.add_event(n, NOW - 100 + n) for n in range(1, 4)]
    server.add_event(9, NOW - 50, published=False)
    collector = _make_collector(client, platform)

    collector.register()
    accepted = collector.poll_once()

    assert accepted > 0
    assert sorted(server.views) == sorted(published)  # unpublished never fetched
    assert len(server.listings) == 2  # 3 events / page size 2
    assert await _entity_count(platform) > 0
    assert collector.cursor is not None
    cursor = MispCursor.parse(collector.cursor)
    assert cursor.publish_timestamp == NOW - 97
    assert set(cursor.recent) == set(published)


async def test_restart_resumes_without_resubmitting(
    misp_server: tuple[MockMisp, TestClient], platform: Platform
) -> None:
    server, client = misp_server
    first_uuids = [server.add_event(n, NOW - 100 + n) for n in (1, 2)]
    first = _make_collector(client, platform)
    first.register()
    first.poll_once()
    count_after_first = await _entity_count(platform)

    # A fresh collector instance (as after a process restart) gets the cursor
    # back from the platform; events inside the overlap window are not repeated.
    new_uuid = server.add_event(3, NOW - 50)
    server.views.clear()
    restarted = _make_collector(client, platform)
    restarted.register()
    assert restarted.cursor == first.cursor

    assert restarted.poll_once() > 0
    assert server.views == [new_uuid]
    assert first_uuids[0] not in server.views
    assert await _entity_count(platform) > count_after_first

    server.views.clear()
    assert restarted.poll_once() == 0
    assert server.views == []


async def test_republished_event_is_fetched_again(
    misp_server: tuple[MockMisp, TestClient], platform: Platform
) -> None:
    server, client = misp_server
    uuid = server.add_event(1, NOW - 100)
    collector = _make_collector(client, platform)
    collector.register()
    collector.poll_once()
    server.views.clear()

    # Republishing bumps publish_timestamp, so the event is submitted again.
    server.events[uuid]["publish_timestamp"] = str(NOW - 10)
    assert collector.poll_once() > 0
    assert server.views == [uuid]
    assert collector.cursor is not None
    assert MispCursor.parse(collector.cursor).recent[uuid] == NOW - 10


async def test_unavailable_event_is_skipped_and_cursor_advances(
    misp_server: tuple[MockMisp, TestClient], platform: Platform
) -> None:
    server, client = misp_server
    gone = server.add_event(1, NOW - 100)
    kept = server.add_event(2, NOW - 90)
    server.hidden_from_view.add(gone)  # listed, but 404 when fetched
    collector = _make_collector(client, platform)
    collector.register()

    assert collector.poll_once() > 0
    assert server.views == [gone, kept]
    assert collector.cursor is not None
    cursor = MispCursor.parse(collector.cursor)
    assert cursor.publish_timestamp == NOW - 90
    assert set(cursor.recent) == {gone, kept}
