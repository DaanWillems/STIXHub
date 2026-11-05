import json
from typing import Any

import httpx
import pytest

from collectors.misp_client import MispClient, MispEventSummary, MispResponseError
from collectors.retry import CollectorStopped


def _event(uuid: str, ts: int | str) -> dict[str, Any]:
    return {"Event": {"uuid": uuid, "publish_timestamp": str(ts)}}


def _client(
    handler: Any,
) -> MispClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return MispClient(http, "https://misp.test/", "secret-key")


def test_list_published_request_and_sorting() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"response": [_event("b", 20), _event("c", 10), _event("a", 20)]}
        )

    result = _client(handler).list_published(
        100, 200, tags=["tlp:green"], org="ORG", page_size=100
    )

    assert result == [
        MispEventSummary("c", 10),
        MispEventSummary("a", 20),
        MispEventSummary("b", 20),
    ]
    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == "https://misp.test/events/restSearch"
    assert request.headers["Authorization"] == "secret-key"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {
        "published": True,
        "metadata": True,
        "publish_timestamp": [100, 200],
        "limit": 100,
        "page": 1,
        "tags": ["tlp:green"],
        "org": "ORG",
    }


def test_list_published_without_lower_bound_or_filters() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"response": []})

    assert _client(handler).list_published(0, 200) == []
    assert bodies[0]["publish_timestamp"] == "<=200"
    assert "tags" not in bodies[0]
    assert "org" not in bodies[0]


def test_list_published_paginates_and_dedupes_across_pages() -> None:
    pages = {
        1: [_event("a", 5), _event("b", 5)],
        2: [_event("b", 5), _event("c", 5)],
        3: [_event("d", 6)],
    }
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["page"]
        seen.append(page)
        return httpx.Response(200, json={"response": pages[page]})

    result = _client(handler).list_published(1, 10, page_size=2)

    assert seen == [1, 2, 3]
    assert [e.uuid for e in result] == ["a", "b", "c", "d"]


def test_list_published_stops_between_pages() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"response": [_event("a", 1), _event("b", 2)]})

    stops = iter([False, True])
    with pytest.raises(CollectorStopped):
        _client(handler).list_published(1, 10, page_size=2, should_stop=lambda: next(stops))
    assert calls == 1


def test_list_published_http_error() -> None:
    client = _client(lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        client.list_published(1, 10)


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"response": {}},
        {"response": [{"nope": 1}]},
        {"response": [{"Event": {"uuid": "a"}}]},
        {"response": [{"Event": {"uuid": "a", "publish_timestamp": "x"}}]},
    ],
)
def test_list_published_malformed(body: Any) -> None:
    client = _client(lambda request: httpx.Response(200, json=body))
    with pytest.raises(MispResponseError):
        client.list_published(1, 10)


def test_list_published_invalid_json() -> None:
    client = _client(lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(MispResponseError):
        client.list_published(1, 10)


def test_get_event() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"Event": {"uuid": "u1", "published": True}})

    event = _client(handler).get_event("u1")

    assert event == {"uuid": "u1", "published": True}
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://misp.test/events/view/u1"
    assert requests[0].headers["Authorization"] == "secret-key"


def test_get_event_not_found_and_malformed() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _client(lambda r: httpx.Response(404)).get_event("u1")
    with pytest.raises(MispResponseError):
        _client(lambda r: httpx.Response(200, json={"Event": []})).get_event("u1")
