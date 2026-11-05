"""End-to-end: mock TAXII 2.1 server -> TaxiiCollector -> collector API -> bucket.

Runs fully in-process (FastAPI TestClient for both the mock TAXII server and
the platform, in-memory repositories), so no database is required.
"""

import base64
import secrets
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.testclient import TestClient

from collectors.platform_client import PlatformClient
from collectors.runner import CollectorRunner
from collectors.taxii_client import TaxiiClient
from collectors.taxii_collector import TaxiiCollector, taxii_timestamp
from dependencies import get_bucket_repo, get_collector_repo
from models.domain import Bucket, CollectorConfig
from database.repositories.bucket import InMemoryBucketRepository
from database.repositories.collector import InMemoryCollectorRepository
from routes.collectors import collectors_router

API_ROOT = "http://taxii.test/api/"
COLLECTION_ID = "91a7b528-80eb-42ed-a74d-c6fbd5a26116"
BUCKET = "external-intel"
CREDENTIALS = ("feed-user", "feed-pass")
PAGE_SIZE = 2


# --- in-process mock TAXII 2.1 server ---------------------------------------


@dataclass
class MockTaxiiServer:
    objects: list[tuple[datetime, dict[str, Any]]] = field(default_factory=list)
    requests: list[dict[str, str]] = field(default_factory=list)
    _clock: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def add_indicators(self, count: int) -> None:
        for _ in range(count):
            self._clock += timedelta(seconds=1)
            n = len(self.objects)
            self.objects.append(
                (
                    self._clock,
                    {
                        "type": "indicator",
                        "spec_version": "2.1",
                        "id": f"indicator--00000000-0000-4000-8000-{n:012d}",
                        "pattern": f"[ipv4-addr:value = '203.0.113.{n}']",
                        "pattern_type": "stix",
                        "valid_from": "2026-01-01T00:00:00Z",
                    },
                )
            )

    def build_app(self) -> FastAPI:
        app = FastAPI()
        basic = HTTPBasic()

        def check_auth(
            creds: Annotated[HTTPBasicCredentials, Depends(basic)],
        ) -> None:
            ok = secrets.compare_digest(
                creds.username, CREDENTIALS[0]
            ) and secrets.compare_digest(creds.password, CREDENTIALS[1])
            if not ok:
                raise HTTPException(status_code=401)

        @app.get(
            "/api/collections/{collection_id}/objects/",
            dependencies=[Depends(check_auth)],
        )
        def get_objects(
            collection_id: str,
            response: Response,
            added_after: str | None = None,
            next_token: Annotated[str | None, Query(alias="next")] = None,
        ) -> dict[str, Any]:
            self.requests.append(
                {"added_after": added_after or "", "next": next_token or ""}
            )
            if collection_id != COLLECTION_ID:
                raise HTTPException(status_code=404)
            matching = self.objects
            if added_after:
                after = datetime.fromisoformat(added_after)
                matching = [(d, o) for d, o in matching if d > after]
            offset = int(base64.b64decode(next_token)) if next_token else 0
            page = matching[offset : offset + PAGE_SIZE]
            more = offset + PAGE_SIZE < len(matching)
            if page:
                response.headers["X-TAXII-Date-Added-First"] = taxii_timestamp(
                    page[0][0]
                )
                response.headers["X-TAXII-Date-Added-Last"] = taxii_timestamp(
                    page[-1][0]
                )
            body: dict[str, Any] = {"more": more, "objects": [o for _, o in page]}
            if more:
                body["next"] = base64.b64encode(
                    str(offset + PAGE_SIZE).encode()
                ).decode()
            return body

        return app


@pytest.fixture
def taxii_server() -> Iterator[tuple[MockTaxiiServer, TestClient]]:
    server = MockTaxiiServer()
    with TestClient(server.build_app(), base_url="http://taxii.test") as client:
        yield server, client


# --- platform under test ----------------------------------------------------


@dataclass
class Platform:
    client: TestClient
    buckets: InMemoryBucketRepository


@pytest.fixture
async def platform() -> Platform:
    buckets = InMemoryBucketRepository()
    await buckets.save(Bucket(name=BUCKET))
    collectors = InMemoryCollectorRepository()
    app = FastAPI()
    app.include_router(collectors_router)
    app.dependency_overrides[get_bucket_repo] = lambda: buckets
    app.dependency_overrides[get_collector_repo] = lambda: collectors
    return Platform(TestClient(app, base_url="http://platform"), buckets)


def _make_collector(
    taxii_client: TestClient,
    platform: Platform,
    stop_event: threading.Event | None = None,
    auth: tuple[str, str] = CREDENTIALS,
) -> TaxiiCollector:
    config = CollectorConfig(
        name="external-feed",
        remote_url=API_ROOT,
        remote_collection_id=COLLECTION_ID,
        target_bucket=BUCKET,
        poll_interval=3600,
        shutdown_timeout=5,
    )
    return TaxiiCollector(
        config,
        TaxiiClient(taxii_client, API_ROOT, COLLECTION_ID, auth=auth),
        PlatformClient(platform.client, "http://platform"),
        stop_event=stop_event,
        backoff_base=0,
    )


async def _bucket_ids(platform: Platform) -> list[str]:
    entities = await platform.buckets.get_entities(bucket_name=BUCKET)
    return sorted(id_ for e in entities for id_ in e.other_stix_ids)


# --- tests -------------------------------------------------------------------


async def test_collects_all_pages_into_bucket(
    taxii_server: tuple[MockTaxiiServer, TestClient], platform: Platform
) -> None:
    server, taxii_client = taxii_server
    server.add_indicators(5)
    collector = _make_collector(taxii_client, platform)

    collector.register()
    accepted = collector.poll_once()

    assert accepted == 5
    assert await _bucket_ids(platform) == sorted(o["id"] for _, o in server.objects)
    assert len(server.requests) == 3  # 5 objects / page size 2
    assert collector.cursor == taxii_timestamp(server.objects[-1][0])


async def test_restart_resumes_from_stored_cursor(
    taxii_server: tuple[MockTaxiiServer, TestClient], platform: Platform
) -> None:
    server, taxii_client = taxii_server
    server.add_indicators(3)
    first = _make_collector(taxii_client, platform)
    first.register()
    first.poll_once()

    # A fresh collector instance (as after a process restart) gets the
    # cursor back from the platform and only fetches newer objects.
    server.add_indicators(2)
    server.requests.clear()
    restarted = _make_collector(taxii_client, platform)
    restarted.register()
    assert restarted.cursor == first.cursor

    assert restarted.poll_once() == 2
    assert server.requests[0]["added_after"] == first.cursor
    assert len(await _bucket_ids(platform)) == 5

    assert restarted.poll_once() == 0
    assert len(await _bucket_ids(platform)) == 5


async def test_bad_credentials_ingest_nothing(
    taxii_server: tuple[MockTaxiiServer, TestClient], platform: Platform
) -> None:
    server, taxii_client = taxii_server
    server.add_indicators(2)
    collector = _make_collector(taxii_client, platform, auth=("feed-user", "wrong"))
    collector.register()

    collector.run_cycle()

    assert len(server.requests) == 0  # rejected by auth before the handler
    assert await _bucket_ids(platform) == []
    assert collector.cursor is None


async def test_threaded_runner_ingests_and_shuts_down(
    taxii_server: tuple[MockTaxiiServer, TestClient], platform: Platform
) -> None:
    server, taxii_client = taxii_server
    server.add_indicators(3)
    stop = threading.Event()
    collector = _make_collector(taxii_client, platform, stop_event=stop)
    runner = CollectorRunner([collector], stop)

    runner.start()
    deadline = time.monotonic() + 5
    while collector.cursor != taxii_timestamp(server.objects[-1][0]):
        assert time.monotonic() < deadline, "collector did not finish polling"
        time.sleep(0.01)
    runner.stop()

    assert not runner.threads[0].is_alive()
    assert len(await _bucket_ids(platform)) == 3
