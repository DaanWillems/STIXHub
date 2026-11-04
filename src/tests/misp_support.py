"""A fake MISP instance and helpers for MISP connector tests."""

import copy
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collectors.misp.client import (
    MispEventRef,
    MispRequestError,
    MispUnavailable,
    index_position,
)
from collectors.misp.connector import MispConnector
from models.domain import MispConnectorConfig
from tests.platform_support import Platform

EVENT_JSON = Path(__file__).parent / "fixtures" / "misp" / "event.json"
EVENT_UUID = "5e8d5c5a-0b1c-4b8e-9d2a-1a2b3c4d5e6f"
# 2026-09-21; every fake MISP timestamp is at or after this.
T0 = 1_790_000_000
# The MISP instance of connector_config(); sync records are keyed by it.
INSTANCE = "https://misp.example.org"
# FakeMisp's organisations: STIXHub's API user's, and everybody else's.
OUR_ORG, THEIR_ORG = "1", "2"
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


class _Hidden(Exception):
    pass


@dataclass
class FakeMisp:
    """An in-memory MISP instance implementing ``MispApi``.

    Like MISP, it stamps every write with a new, increasing ``timestamp``,
    unpublishes an event when it is edited, and publishing sets
    ``publish_timestamp`` but leaves ``timestamp`` alone.
    """

    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    clock: int = T0
    # The organisation of the API key this view of MISP is used with.
    own_org: str = OUR_ORG
    index_filters: list[dict[str, Any]] = field(default_factory=list)
    writes: list[tuple[str, str]] = field(default_factory=list)
    # Errors to raise once from the next "get" / "add" / "update" of an event.
    failures: dict[tuple[str, str], Exception] = field(default_factory=dict)
    # Writes ("add" / "update") to apply but answer as if the response got
    # lost, and how many times.
    lost_responses: dict[tuple[str, str], int] = field(default_factory=dict)

    def fail_next(self, operation: str, uuid: str, error: Exception) -> None:
        self.failures[(operation, uuid)] = error

    def hide_once(self, uuid: str) -> None:
        """Answer the next get of the event as if it didn't exist (yet)."""
        self.fail_next("get", uuid, _Hidden())

    def lose_responses(self, operation: str, uuid: str, times: int = 1) -> None:
        self.lost_responses[(operation, uuid)] = times

    def _maybe_lose_response(self, operation: str, uuid: str) -> None:
        left = self.lost_responses.get((operation, uuid), 0)
        if left:
            self.lost_responses[(operation, uuid)] = left - 1
            raise MispUnavailable("Read timed out")

    def with_key_of(self, org: str) -> "FakeMisp":
        """The same MISP as seen through an API key of organisation ``org``."""
        return replace(self, own_org=org)

    def _maybe_fail(self, operation: str, uuid: str) -> None:
        error = self.failures.pop((operation, uuid), None)
        if error is not None:
            raise error

    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def seed(self, event: dict[str, Any], org: str = THEIR_ORG) -> None:
        """Add an event as if a user of ``org`` created it."""
        self.events[event["uuid"]] = {**copy.deepcopy(event), "orgc_id": org}
        self.clock = max(self.clock, int(event["timestamp"]))

    def edit(self, uuid: str, *, republish: bool = False, **changes: Any) -> None:
        """Change an event as a MISP user would, which unpublishes it, and
        optionally publish it again."""
        self.events[uuid].update(changes, timestamp=str(self._tick()), published=False)
        if republish:
            self.publish(uuid)

    def publish(self, uuid: str) -> None:
        """Publish an event as a MISP user would."""
        self.events[uuid].update(published=True, publish_timestamp=str(self._tick()))

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
        rows = sorted(
            (
                ref
                for ref in map(self._ref, self.events.values())
                if index_position(ref, published_only) >= since
                and (self.events[ref.uuid].get("published") or not published_only)
            ),
            key=lambda ref: index_position(ref, published_only),
        )
        return rows[(page - 1) * limit : page * limit]

    @staticmethod
    def _ref(event: dict[str, Any]) -> MispEventRef:
        return MispEventRef(
            event["uuid"],
            int(event["timestamp"]),
            int(event.get("publish_timestamp") or 0),
        )

    def get_event(self, uuid: str) -> dict[str, Any] | None:
        try:
            self._maybe_fail("get", uuid)
        except _Hidden:
            return None
        event = self.events.get(uuid)
        return copy.deepcopy(event) if event is not None else None

    def add_event(self, event: dict[str, Any]) -> int:
        self._maybe_fail("add", event["uuid"])
        if event["uuid"] in self.events:
            raise MispRequestError(403, "An event with this UUID already exists")
        self.writes.append(("add", event["uuid"]))
        timestamp = self._tick()
        self.events[event["uuid"]] = {
            **copy.deepcopy(event),
            "timestamp": str(timestamp),
            "orgc_id": self.own_org,
        }
        self._maybe_lose_response("add", event["uuid"])
        return timestamp

    def update_event(self, uuid: str, event: dict[str, Any]) -> int:
        self._maybe_fail("update", uuid)
        if uuid not in self.events:
            raise MispRequestError(404, "Invalid event")
        self.writes.append(("update", uuid))
        timestamp = self._tick()
        self.events[uuid] = {
            **copy.deepcopy(event),
            "timestamp": str(timestamp),
            "orgc_id": self.events[uuid]["orgc_id"],
        }
        self._maybe_lose_response("update", uuid)
        return timestamp

    def own_org_id(self) -> str:
        return self.own_org

    def publish_event(self, uuid: str) -> None:
        self.writes.append(("publish", uuid))
        self.publish(uuid)


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
    if isinstance(values["push"], dict):
        # Push at once; the tests for waiting set it themselves.
        values["push"] = {"wait_for_refs_seconds": 0, **values["push"]}
    return MispConnectorConfig.model_validate(values)


def make_connector(
    config: MispConnectorConfig,
    misp: FakeMisp,
    platform: Platform,
    excluded_creators: frozenset[str] = frozenset(),
    clock: Callable[[], datetime] = lambda: NOW,
) -> MispConnector:
    platform.enroll(config)
    connector = MispConnector(
        config,
        misp,
        platform.api(config.name),
        excluded_creators=excluded_creators,
        stop_event=threading.Event(),
        backoff_base=0,
        clock=clock,
    )
    connector.register()
    return connector
