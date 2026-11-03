from typing import Any

import pytest
import requests
from pymisp.exceptions import MISPServerError, PyMISPError

from collectors.misp.client import (
    MispEventRef,
    MispRequestError,
    MispUnavailable,
    PyMispClient,
)
from collectors.retry import is_transient


class StubPyMisp:
    """Answers like PyMISP: 4xx as an ``errors`` dict, 5xx as an exception."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _respond(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def search_index(self, **kwargs: Any) -> Any:
        return self._respond("search_index", **kwargs)

    def get_event(self, uuid: str, **kwargs: Any) -> Any:
        return self._respond("get_event", uuid=uuid, **kwargs)

    def add_event(self, event: dict[str, Any], **kwargs: Any) -> Any:
        return self._respond("add_event", event=event, **kwargs)


def _client(answer: Any) -> tuple[PyMispClient, StubPyMisp]:
    stub = StubPyMisp(answer)

    def connect(*args: Any, **kwargs: Any) -> Any:
        return stub

    return PyMispClient("https://misp", "key", True, 5, connect=connect), stub


def test_index_rows_become_refs_and_filters_are_passed() -> None:
    row = {"uuid": "e1", "timestamp": "1790000000", "publish_timestamp": "1790000005"}
    client, stub = _client([row])

    refs = client.index_events(
        since=1, published_only=True, tags=["tlp:green"], orgs=[], page=2, limit=50
    )

    assert refs == [MispEventRef("e1", 1790000000, 1790000005)]
    assert stub.calls[0][1] == {
        "publish_timestamp": 1,
        "published": True,
        "tags": ["tlp:green"],
        "org": None,
        "sort": "publish_timestamp",
        "desc": False,
        "page": 2,
        "limit": 50,
        "pythonify": False,
    }


def test_all_events_are_listed_by_modification_time() -> None:
    client, stub = _client([{"uuid": "e1", "timestamp": "7", "publish_timestamp": "0"}])

    refs = client.index_events(
        since=1, published_only=False, tags=[], orgs=[], page=1, limit=50
    )

    assert refs == [MispEventRef("e1", 7, 0)]
    assert stub.calls[0][1]["timestamp"] == 1
    assert stub.calls[0][1]["sort"] == "timestamp"
    assert stub.calls[0][1]["published"] is None


def test_event_is_unwrapped_and_missing_event_is_none() -> None:
    assert _client({"Event": {"uuid": "e1"}})[0].get_event("e1") == {"uuid": "e1"}
    assert (
        _client({"errors": (404, {"message": "Invalid event"})})[0].get_event("e1")
        is None
    )


def test_add_returns_the_new_timestamp() -> None:
    client, _ = _client({"Event": {"uuid": "e1", "timestamp": "1790000001"}})
    assert client.add_event({"uuid": "e1"}) == 1790000001


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("refused"),
        requests.Timeout("slow"),
        MISPServerError("Error code 500"),
    ],
)
def test_unreachable_or_failing_misp_is_transient(failure: Exception) -> None:
    client, _ = _client(failure)
    with pytest.raises(MispUnavailable) as raised:
        client.get_event("e1")
    assert is_transient(raised.value)


def test_refused_request_is_permanent() -> None:
    client, _ = _client({"errors": (403, {"message": "Not allowed"})})
    with pytest.raises(MispRequestError) as raised:
        client.add_event({"uuid": "e1"})
    assert raised.value.status_code == 403
    assert not is_transient(raised.value)


def test_connection_failure_on_connect_is_transient() -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise PyMISPError("Unable to connect to MISP")

    client = PyMispClient("https://misp", "key", True, 5, connect=refuse)
    with pytest.raises(MispUnavailable):
        client.get_event("e1")
