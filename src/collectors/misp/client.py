"""Typed access to a MISP instance.

``MispApi`` is what the pull and push steps use; ``PyMispClient`` implements it
with PyMISP, and tests use an in-memory fake. Only this module talks to PyMISP.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import requests
from pymisp import PyMISP, PyMISPError
from pymisp.exceptions import MISPServerError

from collectors.retry import TransientError

logger = logging.getLogger(__name__)


class MispUnavailable(TransientError):
    """MISP could not be reached or failed server-side (worth retrying)."""


# 4xx statuses that mean "not now" rather than "no": timeout, rate limit.
_RETRYABLE_STATUSES = frozenset({408, 429})


class MispRequestError(Exception):
    """MISP rejected a request (HTTP 4xx); retrying won't help."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"MISP returned HTTP {status_code}: {detail}")
        self.status_code = status_code


@dataclass(frozen=True)
class MispEventRef:
    """An event as MISP's event index lists it."""

    uuid: str
    timestamp: int  # last modified
    publish_timestamp: int  # last published; 0 if never


def index_position(ref: MispEventRef, published_only: bool) -> int:
    """Where ``ref`` sits in the index ``index_events`` lists.

    Publishing an event moves its ``publish_timestamp`` but not its
    ``timestamp``, so a pull of published events must follow the former.
    """
    return ref.publish_timestamp if published_only else ref.timestamp


class MispApi(Protocol):
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
        """One page of the events at or after ``since``, oldest first.

        With ``published_only`` that is the published events by
        ``publish_timestamp``; otherwise every event by ``timestamp``.
        """

    def get_event(self, uuid: str) -> dict[str, Any] | None:
        """The full event (unwrapped from ``Event``) without deleted attributes,
        or ``None`` if it doesn't exist."""

    def add_event(self, event: dict[str, Any]) -> int:
        """Create the event; returns its new MISP timestamp."""

    def update_event(self, uuid: str, event: dict[str, Any]) -> int:
        """Replace the event's content; returns its new MISP timestamp."""

    def publish_event(self, uuid: str) -> None: ...

    def own_org_id(self) -> str:
        """The organisation of the API key's user: the creator of our events."""


def created_by(event: dict[str, Any], org_id: str) -> bool:
    """Whether organisation ``org_id`` created ``event``."""
    creator = event.get("orgc_id") or event.get("Orgc", {}).get("id")
    return str(creator) == org_id


def event_timestamp(event: dict[str, Any]) -> int:
    """The ``timestamp`` of an event dict, wrapped in ``Event`` or not."""
    return int(event.get("Event", event)["timestamp"])


class PyMispClient:
    """``MispApi`` over PyMISP.

    PyMISP contacts the server when it is constructed, so that happens lazily,
    on the collector thread, where connection failures are retried.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        verify_tls: bool | str,
        timeout: float,
        connect: Callable[..., PyMISP] = PyMISP,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._connect = connect
        self._misp: PyMISP | None = None
        self._org_id: str | None = None

    def _api(self) -> PyMISP:
        if self._misp is None:
            try:
                self._misp = self._connect(
                    self._url,
                    self._api_key,
                    ssl=self._verify_tls,
                    timeout=self._timeout,
                    tool="STIXHub",
                )
            except PyMISPError as exc:
                # PyMISP reports an unreachable server and a rejected key
                # alike, so both are retried.
                raise MispUnavailable(f"Cannot connect to MISP: {exc}") from exc
        return self._misp

    def _call(
        self, description: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Call PyMISP, turning its error reporting into exceptions."""
        try:
            result = fn(*args, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise MispUnavailable(f"{description}: {exc}") from exc
        except MISPServerError as exc:
            raise MispUnavailable(f"{description}: {exc}") from exc
        if isinstance(result, dict) and "errors" in result:
            errors = result["errors"]
            if isinstance(errors, (tuple, list)) and len(errors) == 2:
                status = int(errors[0])
                if status in _RETRYABLE_STATUSES:
                    raise MispUnavailable(f"{description}: HTTP {status}")
                raise MispRequestError(status, errors[1])
            raise MispUnavailable(f"{description}: {errors}")
        return result

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
        api = self._api()
        position = "publish_timestamp" if published_only else "timestamp"
        rows = self._call(
            "search event index",
            api.search_index,
            published=True if published_only else None,
            tags=tags or None,
            org=orgs or None,
            sort=position,
            desc=False,
            page=page,
            limit=limit,
            pythonify=False,
            **{position: since},
        )
        return [
            MispEventRef(
                uuid=row["uuid"],
                timestamp=int(row["timestamp"]),
                publish_timestamp=int(row.get("publish_timestamp") or 0),
            )
            for row in rows
        ]

    def get_event(self, uuid: str) -> dict[str, Any] | None:
        api = self._api()
        try:
            found: dict[str, Any] = self._call(
                f"get event {uuid}", api.get_event, uuid, pythonify=False
            )
        except MispRequestError as exc:
            if exc.status_code == 404:
                return None
            raise
        event: dict[str, Any] = found.get("Event", found)
        return event

    def add_event(self, event: dict[str, Any]) -> int:
        api = self._api()
        return event_timestamp(
            self._call("add event", api.add_event, event, pythonify=False)
        )

    def update_event(self, uuid: str, event: dict[str, Any]) -> int:
        api = self._api()
        return event_timestamp(
            self._call(
                f"update event {uuid}",
                api.update_event,
                event,
                event_id=uuid,
                pythonify=False,
            )
        )

    def publish_event(self, uuid: str) -> None:
        api = self._api()
        self._call(f"publish event {uuid}", api.publish, uuid)

    def own_org_id(self) -> str:
        if self._org_id is None:
            api = self._api()
            me = self._call("get own user", api.get_user, "me", pythonify=False)
            self._org_id = str(me.get("User", me)["org_id"])
        return self._org_id

    def close(self) -> None:
        # PyMISP has no close(); dropping it releases its HTTP session.
        self._misp = None
