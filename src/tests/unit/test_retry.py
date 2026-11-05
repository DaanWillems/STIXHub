from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from collectors.retry import (
    CollectorStopped,
    RetriesExhausted,
    is_transient,
    retry_after_seconds,
    with_retries,
)


def _status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://misp.test/")
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, True), (500, True), (503, True), (400, False), (401, False), (404, False)],
)
def test_is_transient_status_codes(status: int, expected: bool) -> None:
    assert is_transient(_status_error(status)) is expected


def test_retry_after_seconds_parses_delta() -> None:
    assert retry_after_seconds(_status_error(429, {"Retry-After": "7"})) == 7.0


def test_retry_after_seconds_parses_http_date() -> None:
    when = datetime.now(UTC) + timedelta(seconds=30)
    exc = _status_error(429, {"Retry-After": format_datetime(when, usegmt=True)})
    value = retry_after_seconds(exc)
    assert value is not None
    assert 20 < value <= 30


@pytest.mark.parametrize("header", ["soon", "-5", "nan", ""])
def test_retry_after_seconds_ignores_invalid(header: str) -> None:
    assert retry_after_seconds(_status_error(429, {"Retry-After": header})) is None


def test_retry_after_seconds_only_for_429_and_http_errors() -> None:
    assert retry_after_seconds(_status_error(503, {"Retry-After": "5"})) is None
    assert retry_after_seconds(_status_error(429)) is None
    assert retry_after_seconds(ValueError("x")) is None


def _run(errors: list[Exception], max_delay: float = 60.0) -> list[float]:
    delays: list[float] = []
    queue = list(errors)

    def fn() -> str:
        if queue:
            raise queue.pop(0)
        return "ok"

    def wait(delay: float) -> bool:
        delays.append(delay)
        return False

    result = with_retries(
        fn,
        description="t",
        max_retries=len(errors) + 1,
        wait=wait,
        base_delay=1.0,
        max_delay=max_delay,
    )
    assert result == "ok"
    return delays


def test_429_is_retried_and_retry_after_replaces_backoff() -> None:
    delays = _run([_status_error(429, {"Retry-After": "12"}), _status_error(429)])
    assert delays == [12.0, 2.0]


def test_retry_after_is_capped_at_max_delay() -> None:
    delays = _run([_status_error(429, {"Retry-After": "3600"})], max_delay=30.0)
    assert delays == [30.0]


def test_429_exhausts_retries() -> None:
    with pytest.raises(RetriesExhausted):
        with_retries(
            lambda: (_ for _ in ()).throw(_status_error(429)),
            description="t",
            max_retries=2,
            wait=lambda _d: False,
        )


def test_stop_during_retry_after_wait() -> None:
    with pytest.raises(CollectorStopped):
        with_retries(
            lambda: (_ for _ in ()).throw(_status_error(429, {"Retry-After": "5"})),
            description="t",
            max_retries=3,
            wait=lambda _d: True,
        )
