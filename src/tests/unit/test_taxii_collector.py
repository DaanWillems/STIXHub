import base64
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from collectors.platform_client import PlatformClient
from collectors.retry import RetriesExhausted, with_retries
from collectors.taxii_client import TAXII_MEDIA_TYPE, TaxiiClient
from collectors.taxii_collector import RegistrationError, TaxiiCollector
from models.domain import CollectorConfig

API_ROOT = "https://taxii.example.com/api/"
PLATFORM = "http://platform"
PLATFORM_SECRET = "platform-secret"
OBJECTS_PATH = "/api/collections/coll-1/objects/"
FIXED_NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


def _obj(n: int) -> dict[str, Any]:
    return {"type": "indicator", "id": f"indicator--{n}", "spec_version": "2.1"}


def _page(
    objects: list[dict[str, Any]],
    more: bool = False,
    next_token: str | None = None,
    date_added_last: str | None = None,
    status: int = 200,
) -> httpx.Response:
    body: dict[str, Any] = {"objects": objects, "more": more}
    if next_token:
        body["next"] = next_token
    headers = {"X-TAXII-Date-Added-Last": date_added_last} if date_added_last else {}
    return httpx.Response(status, json=body, headers=headers)


@dataclass
class FakePlatform:
    """Records calls to the platform's collector API."""

    submitted: list[list[dict[str, Any]]] = field(default_factory=list)
    cursors: list[str] = field(default_factory=list)
    register_status: int = 200
    submit_status: int = 202
    fail_ids: set[str] = field(default_factory=set)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {PLATFORM_SECRET}"
        path = request.url.path
        if path == "/collectors/register":
            if self.register_status != 200:
                return httpx.Response(self.register_status, json={"detail": "nope"})
            return httpx.Response(
                200, json={"id": 7, "name": "feed", "bucket_id": 1, "cursor": None}
            )
        if path == "/collectors/7/objects":
            if self.submit_status != 202:
                return httpx.Response(self.submit_status)
            objects = json.loads(request.content)["objects"]
            self.submitted.append(objects)
            ok = [o for o in objects if o["id"] not in self.fail_ids]
            bad = [o for o in objects if o["id"] in self.fail_ids]
            return httpx.Response(
                202,
                json={
                    "id": "s",
                    "status": "complete_with_errors" if bad else "complete",
                    "request_timestamp": "now",
                    "total_count": len(objects),
                    "success_count": len(ok),
                    "failure_count": len(bad),
                    "successes": [{"id": o["id"]} for o in ok],
                    "failures": [{"id": o["id"], "message": "bad"} for o in bad],
                },
            )
        if path == "/collectors/7/cursor":
            self.cursors.append(json.loads(request.content)["cursor"])
            return httpx.Response(200, json={"id": 7, "name": "feed", "bucket_id": 1})
        return httpx.Response(404)


@dataclass
class FakeTaxii:
    """Serves scripted responses and records each request."""

    responses: list[httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == OBJECTS_PATH
        self.requests.append(request)
        return self.responses.pop(0)


def _collector(
    taxii: FakeTaxii,
    platform: FakePlatform,
    auth: tuple[str, str] | None = None,
    max_retries: int = 3,
) -> TaxiiCollector:
    config = CollectorConfig(
        name="feed",
        remote_url=API_ROOT,
        remote_collection_id="coll-1",
        target_bucket="intel",
        secret_env="FEED",
        max_retries=max_retries,
    )
    taxii_client = TaxiiClient(
        httpx.Client(transport=httpx.MockTransport(taxii.handle)),
        API_ROOT,
        "coll-1",
        auth=auth,
    )
    platform_client = PlatformClient(
        httpx.Client(transport=httpx.MockTransport(platform.handle)),
        PLATFORM,
        PLATFORM_SECRET,
    )
    return TaxiiCollector(
        config,
        taxii_client,
        platform_client,
        stop_event=threading.Event(),
        backoff_base=0,
        clock=lambda: FIXED_NOW,
    )


# --- pagination & cursor management ---


def test_drains_all_pages_and_advances_cursor_per_page() -> None:
    taxii = FakeTaxii(
        [
            _page([_obj(1), _obj(2)], True, "tok-1", "2026-01-01T00:00:01.000Z"),
            _page([_obj(3)], False, None, "2026-01-01T00:00:02.000Z"),
        ]
    )
    platform = FakePlatform()
    collector = _collector(taxii, platform)
    collector.register()
    collector.cursor = "2025-12-31T00:00:00.000Z"

    accepted = collector.poll_once()

    assert accepted == 3
    assert [len(batch) for batch in platform.submitted] == [2, 1]
    assert platform.cursors == ["2026-01-01T00:00:01.000Z", "2026-01-01T00:00:02.000Z"]
    assert collector.cursor == "2026-01-01T00:00:02.000Z"
    # Every page in the cycle uses the cursor from the start of the cycle.
    first, second = taxii.requests
    assert first.url.params["added_after"] == "2025-12-31T00:00:00.000Z"
    assert "next" not in first.url.params
    assert second.url.params["added_after"] == "2025-12-31T00:00:00.000Z"
    assert second.url.params["next"] == "tok-1"


def test_first_poll_without_cursor_omits_added_after() -> None:
    taxii = FakeTaxii([_page([_obj(1)], date_added_last="2026-01-01T00:00:00Z")])
    collector = _collector(taxii, FakePlatform())
    collector.register()

    collector.poll_once()

    assert "added_after" not in taxii.requests[0].url.params


def test_cursor_falls_back_to_now_without_header() -> None:
    platform = FakePlatform()
    collector = _collector(FakeTaxii([_page([_obj(1)])]), platform)
    collector.register()

    collector.poll_once()

    assert platform.cursors == ["2026-03-01T12:00:00.000000Z"]


def test_empty_page_submits_nothing_and_keeps_cursor() -> None:
    platform = FakePlatform()
    collector = _collector(FakeTaxii([_page([])]), platform)
    collector.register()
    collector.cursor = "2026-01-01T00:00:00Z"

    assert collector.poll_once() == 0
    assert platform.submitted == []
    assert platform.cursors == []
    assert collector.cursor == "2026-01-01T00:00:00Z"


def test_partial_failure_still_advances_cursor() -> None:
    platform = FakePlatform(fail_ids={"indicator--2"})
    collector = _collector(
        FakeTaxii([_page([_obj(1), _obj(2)], date_added_last="2026-01-02T00:00:00Z")]),
        platform,
    )
    collector.register()

    assert collector.poll_once() == 1
    assert platform.cursors == ["2026-01-02T00:00:00Z"]


def test_batch_with_no_accepted_objects_does_not_advance_cursor() -> None:
    platform = FakePlatform(fail_ids={"indicator--1"})
    collector = _collector(
        FakeTaxii([_page([_obj(1)], date_added_last="2026-01-02T00:00:00Z")]),
        platform,
    )
    collector.register()

    assert collector.poll_once() == 0
    assert platform.cursors == []
    assert collector.cursor is None


def test_platform_failure_leaves_cursor_unchanged() -> None:
    platform = FakePlatform(submit_status=503)
    collector = _collector(
        FakeTaxii([_page([_obj(1)], date_added_last="2026-01-02T00:00:00Z")]),
        platform,
    )
    collector.register()
    collector.cursor = "2026-01-01T00:00:00Z"

    collector.run_cycle()  # logs RetriesExhausted, does not raise

    assert platform.cursors == []
    assert collector.cursor == "2026-01-01T00:00:00Z"


# --- retry behaviour ---


def test_transient_taxii_error_is_retried() -> None:
    taxii = FakeTaxii(
        [
            httpx.Response(503),
            httpx.Response(502),
            _page([_obj(1)], date_added_last="2026-01-02T00:00:00Z"),
        ]
    )
    platform = FakePlatform()
    collector = _collector(taxii, platform, max_retries=3)
    collector.register()

    assert collector.poll_once() == 1
    assert len(taxii.requests) == 3


def test_retries_give_up_after_max_retries() -> None:
    taxii = FakeTaxii([httpx.Response(500) for _ in range(5)])
    collector = _collector(taxii, FakePlatform(), max_retries=2)
    collector.register()

    with pytest.raises(RetriesExhausted):
        collector.poll_once()
    assert len(taxii.requests) == 2


def test_taxii_4xx_is_permanent_and_not_retried() -> None:
    taxii = FakeTaxii([httpx.Response(401), _page([_obj(1)])])
    platform = FakePlatform()
    collector = _collector(taxii, platform, max_retries=3)
    collector.register()

    collector.run_cycle()  # logs the permanent failure, does not raise

    assert len(taxii.requests) == 1
    assert platform.submitted == []


def test_with_retries_uses_exponential_backoff() -> None:
    delays: list[float] = []
    calls = 0

    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise httpx.ConnectTimeout("timeout")
        return "ok"

    def wait(delay: float) -> bool:
        delays.append(delay)
        return False

    result = with_retries(
        flaky, description="test", max_retries=4, wait=wait, base_delay=1.5
    )

    assert result == "ok"
    assert delays == [1.5, 3.0, 6.0]


# --- wire format, auth & registration ---


def test_sends_taxii_accept_header_and_basic_auth() -> None:
    taxii = FakeTaxii([_page([])])
    collector = _collector(taxii, FakePlatform(), auth=("alice", "s3cret"))
    collector.register()

    collector.poll_once()

    request = taxii.requests[0]
    assert request.headers["Accept"] == TAXII_MEDIA_TYPE
    expected = base64.b64encode(b"alice:s3cret").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


def test_registration_refused_is_fatal() -> None:
    collector = _collector(FakeTaxii([]), FakePlatform(register_status=404))
    fatal: list[BaseException] = []

    collector.run(on_fatal=fatal.append)

    assert len(fatal) == 1
    assert isinstance(fatal[0], RegistrationError)
    assert "intel" in str(fatal[0])
