import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from collectors import misp_collector
from collectors.base import RegistrationError
from collectors.misp_client import MispClient, MispEventSummary
from collectors.misp_collector import MispCollector
from collectors.misp_cursor import MispCursor
from collectors.platform_client import PlatformClient
from collectors.retry import RetriesExhausted
from models.domain import CollectorResponse, MispCollectorConfig, TaxiiWriteStatusModel

NOW = 1_800_000_000


def clock() -> datetime:
    return datetime.fromtimestamp(NOW, UTC)


def _status(code: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://misp")
    return httpx.HTTPStatusError(
        "err",
        request=request,
        response=httpx.Response(code, text=text, request=request),
    )


class FakeMisp(MispClient):
    def __init__(self) -> None:
        super().__init__(httpx.Client(), "http://misp", "key")
        self.listing: list[MispEventSummary] = []
        self.events: dict[str, dict[str, Any] | Exception] = {}
        self.list_error: Exception | None = None
        self.list_calls: list[tuple[int, int]] = []

    def list_published(
        self, window_start: int, window_end: int, *args: Any, **kwargs: Any
    ) -> list[MispEventSummary]:
        self.list_calls.append((window_start, window_end))
        if self.list_error:
            raise self.list_error
        return list(self.listing)

    def get_event(self, uuid: str) -> dict[str, Any]:
        result = self.events[uuid]
        if isinstance(result, Exception):
            raise result
        return result


class FakePlatform(PlatformClient):
    def __init__(self, cursor: str | None = None) -> None:
        super().__init__(httpx.Client(), "http://platform", "s")
        self.stored_cursor = cursor
        self.submitted: list[list[dict[str, Any]]] = []
        self.cursors: list[str] = []
        self.submit_error: Exception | None = None
        self.success_count: int | None = None

    def register(self, name: str, bucket_name: str) -> CollectorResponse:
        return CollectorResponse(
            id=1, name=name, bucket_id=1, cursor=self.stored_cursor
        )

    def submit_objects(
        self, collector_id: int, objects: list[dict[str, Any]]
    ) -> TaxiiWriteStatusModel:
        if self.submit_error:
            raise self.submit_error
        self.submitted.append(objects)
        count = len(objects) if self.success_count is None else self.success_count
        return TaxiiWriteStatusModel(
            id="s",
            status="complete",
            request_timestamp="t",
            total_count=len(objects),
            success_count=count,
            failure_count=len(objects) - count,
        )

    def update_cursor(self, collector_id: int, cursor: str) -> None:
        self.cursors.append(cursor)


def _event(uuid: str, published: bool = True) -> dict[str, Any]:
    return {"uuid": uuid, "published": published}


def _make(
    cursor: str | None = None, **overrides: Any
) -> tuple[MispCollector, FakeMisp, FakePlatform]:
    config = MispCollectorConfig(
        name="m",
        type="misp",
        misp_url="http://misp",
        api_key_env="K",
        secret_env="S",
        target_bucket="b",
        submit_batch_size=2,
        max_retries=2,
        **overrides,
    )
    misp, platform = FakeMisp(), FakePlatform(cursor)
    collector = MispCollector(config, misp, platform, backoff_base=0, clock=clock)
    collector.register()
    return collector, misp, platform


@pytest.fixture(autouse=True)
def fake_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    def convert(event: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"type": "indicator", "id": f"indicator--{event['uuid']}-{i}"}
            for i in range(3)
        ]

    monkeypatch.setattr(misp_collector, "convert_event", convert)


def _list(misp: FakeMisp, *entries: tuple[str, int]) -> None:
    misp.listing = [MispEventSummary(u, t) for u, t in entries]
    for u, _ in entries:
        misp.events.setdefault(u, _event(u))


def _cursor(platform: FakePlatform) -> MispCursor:
    return MispCursor.parse(platform.cursors[-1])


def test_unparseable_cursor_fails_registration() -> None:
    with pytest.raises(RegistrationError, match="reset"):
        _make(cursor="2026-01-01T00:00:00Z")


def test_ingests_in_chunks_and_advances_cursor() -> None:
    collector, misp, platform = _make(cursor=MispCursor(1000).serialize())
    _list(misp, ("a", 1500), ("b", 1600))
    assert collector.poll_once() == 6
    assert [len(c) for c in platform.submitted] == [2, 1, 2, 1]
    assert _cursor(platform).publish_timestamp == 1600
    assert collector.cursor == platform.cursors[-1]
    assert misp.list_calls == [(0, NOW)]


def test_first_run_applies_lookback_and_persists_cursor_when_empty() -> None:
    collector, misp, platform = _make(initial_lookback_days=1, overlap_seconds=0)
    assert collector.poll_once() == 0
    assert misp.list_calls == [(NOW - 86400, NOW)]
    assert _cursor(platform).publish_timestamp == NOW - 86400
    misp.list_calls.clear()
    collector.poll_once()
    assert misp.list_calls == [(NOW - 86400, NOW)]  # not re-applied relative to now


def test_already_handled_event_is_skipped() -> None:
    cursor = MispCursor(1500, {"a": 1500}).serialize()
    collector, misp, platform = _make(cursor=cursor)
    _list(misp, ("a", 1500), ("b", 1500))
    assert collector.poll_once() == 3
    assert len(platform.submitted) == 2 and "b" in platform.submitted[0][0]["id"]


@pytest.mark.parametrize(
    "event",
    [_event("other"), _event("a", published=False)],
)
def test_changed_event_is_skipped_but_cursor_advances(event: Any) -> None:
    collector, misp, platform = _make(cursor=MispCursor(0).serialize())
    _list(misp, ("a", 10), ("b", 20))
    misp.events["a"] = event
    assert collector.poll_once() == 3
    assert _cursor(platform).publish_timestamp == 20


@pytest.mark.parametrize("code", [403, 404])
def test_event_http_error_skips_only_that_event(code: int) -> None:
    collector, misp, platform = _make(cursor=MispCursor(0).serialize())
    _list(misp, ("a", 10), ("b", 20))
    misp.events["a"] = _status(code)
    assert collector.poll_once() == 3
    assert _cursor(platform).publish_timestamp == 20


@pytest.mark.parametrize("code", [401, 403])
def test_listing_auth_failure_aborts_poll(code: int) -> None:
    collector, misp, platform = _make(cursor=MispCursor(0).serialize())
    misp.list_error = _status(code)
    assert collector.poll_once() == 0
    assert platform.cursors == [] and platform.submitted == []


def test_listing_server_error_exhausts_retries() -> None:
    collector, misp, _ = _make(cursor=MispCursor(0).serialize())
    misp.list_error = _status(500)
    with pytest.raises(RetriesExhausted):
        collector.poll_once()


def test_platform_4xx_skips_event_and_advances() -> None:
    collector, misp, platform = _make(cursor=MispCursor(0).serialize())
    _list(misp, ("a", 10))
    platform.submit_error = _status(422, "bad")
    assert collector.poll_once() == 0
    assert _cursor(platform).publish_timestamp == 10


def test_zero_successes_still_advances() -> None:
    collector, misp, platform = _make(cursor=MispCursor(0).serialize())
    _list(misp, ("a", 10))
    platform.success_count = 0
    assert collector.poll_once() == 0
    assert _cursor(platform).publish_timestamp == 10


def test_platform_5xx_does_not_advance() -> None:
    collector, misp, platform = _make(cursor=MispCursor(0).serialize())
    _list(misp, ("a", 10))
    platform.submit_error = _status(503)
    with pytest.raises(RetriesExhausted):
        collector.poll_once()
    assert platform.cursors == []


def test_conversion_failure_and_empty_result_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def convert(event: dict[str, Any]) -> list[dict[str, Any]]:
        if event["uuid"] == "a":
            raise ValueError("boom")
        return []

    monkeypatch.setattr(misp_collector, "convert_event", convert)
    collector, misp, platform = _make(cursor=MispCursor(0).serialize())
    _list(misp, ("a", 10), ("b", 20))
    assert collector.poll_once() == 0
    assert platform.submitted == []
    assert json.loads(platform.cursors[-1])["publish_timestamp"] == 20
