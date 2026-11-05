from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from collectors.retry import CollectorStopped


class MispResponseError(Exception):
    """Raised when MISP returns a response that does not have the expected shape."""


@dataclass(frozen=True)
class MispEventSummary:
    uuid: str
    publish_timestamp: int


class MispClient:
    """Minimal synchronous MISP client: list published events and fetch one event."""

    def __init__(self, http: httpx.Client, base_url: str, api_key: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def list_published(
        self,
        window_start: int,
        window_end: int,
        tags: Sequence[str] = (),
        org: str | None = None,
        page_size: int = 100,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> list[MispEventSummary]:
        """List published events with ``window_start <= publish_timestamp <= window_end``.

        Pages are deduplicated by uuid and the result is sorted by
        ``(publish_timestamp, uuid)``. A ``window_start`` of 0 leaves out the lower
        bound. ``should_stop`` is checked between pages; when it returns ``True``
        :class:`CollectorStopped` is raised. Raises ``httpx.HTTPStatusError`` on
        4xx/5xx and :class:`MispResponseError` on a malformed body.
        """
        found: dict[str, MispEventSummary] = {}
        page = 1
        while True:
            if should_stop():
                raise CollectorStopped
            body: dict[str, Any] = {
                "published": True,
                "metadata": True,
                "publish_timestamp": (
                    [window_start, window_end] if window_start > 0 else f"<={window_end}"
                ),
                "limit": page_size,
                "page": page,
            }
            if tags:
                body["tags"] = list(tags)
            if org:
                body["org"] = org
            response = self._http.post(
                f"{self._base_url}/events/restSearch", json=body, headers=self._headers
            )
            response.raise_for_status()
            entries = self._parse_listing(response)
            for entry in entries:
                found.setdefault(entry.uuid, entry)
            if len(entries) < page_size:
                break
            page += 1
        return sorted(found.values(), key=lambda e: (e.publish_timestamp, e.uuid))

    def get_event(self, uuid: str) -> dict[str, Any]:
        """Fetch one event and return its ``Event`` dict.

        Raises ``httpx.HTTPStatusError`` on 4xx/5xx (404 when the event is gone or
        not visible) and :class:`MispResponseError` on a malformed body.
        """
        response = self._http.get(
            f"{self._base_url}/events/view/{uuid}", headers=self._headers
        )
        response.raise_for_status()
        event = _json(response).get("Event")
        if not isinstance(event, dict):
            raise MispResponseError("MISP event response has no 'Event' object")
        return event

    @staticmethod
    def _parse_listing(response: httpx.Response) -> list[MispEventSummary]:
        items = _json(response).get("response")
        if not isinstance(items, list):
            raise MispResponseError("MISP restSearch response has no 'response' list")
        entries: list[MispEventSummary] = []
        for item in items:
            event = item.get("Event") if isinstance(item, dict) else None
            if not isinstance(event, dict):
                raise MispResponseError("MISP restSearch item has no 'Event' object")
            try:
                # MISP serialises numeric fields as strings.
                entries.append(
                    MispEventSummary(
                        uuid=str(event["uuid"]),
                        publish_timestamp=int(event["publish_timestamp"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MispResponseError(
                    f"MISP event listing entry is invalid: {exc!r}"
                ) from exc
        return entries


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise MispResponseError("MISP response is not valid JSON") from exc
    if not isinstance(body, dict):
        raise MispResponseError("MISP response is not a JSON object")
    return body
