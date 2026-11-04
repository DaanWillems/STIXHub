"""Pull: MISP events -> STIX objects -> the pull bucket."""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import partial

from collectors.misp.client import (
    MispApi,
    MispEventRef,
    created_by,
    index_position,
)
from collectors.misp.common import Retrier, load_cursor, register
from collectors.misp.convert import ConversionError, event_to_stix
from collectors.platform_client import PlatformClient
from collectors.retry import CollectorStopped
from models.domain import (
    MispConnectorConfig,
    MispEventOrigin,
    MispEventRecord,
    MispPullConfig,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PullCursor:
    """Where pulling resumes: an index position (see ``index_position``).

    MISP's index filters are inclusive and have one-second resolution, so the
    cursor also remembers which events at exactly ``timestamp`` were done.
    """

    timestamp: int
    uuids: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def parse(cls, raw: str) -> "PullCursor":
        data = json.loads(raw)
        return cls(int(data["timestamp"]), frozenset(data["uuids"]))

    def dump(self) -> str:
        return json.dumps({"timestamp": self.timestamp, "uuids": sorted(self.uuids)})

    def covers(self, position: int, uuid: str) -> bool:
        return position < self.timestamp or (
            position == self.timestamp and uuid in self.uuids
        )

    def past(self, position: int, uuid: str) -> "PullCursor":
        if position > self.timestamp:
            return PullCursor(position, frozenset({uuid}))
        return PullCursor(self.timestamp, self.uuids | {uuid})


class _Outcome(Enum):
    INGESTED = "ingested"
    # Permanently unusable (gone, or misp-stix can't convert it): move past it.
    SKIPPED = "skipped"
    # The platform accepted none of its objects: stop and retry next cycle.
    REJECTED = "rejected"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MispPuller:
    def __init__(
        self,
        config: MispConnectorConfig,
        misp: MispApi,
        platform: PlatformClient,
        retry: Retrier,
        stop: threading.Event,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        assert config.pull is not None
        self._name = config.name
        self._pull: MispPullConfig = config.pull
        self._misp = misp
        self._platform = platform
        self._retry = retry
        self._stop = stop
        self._clock = clock
        self.collector_id: int | None = None
        self.cursor: PullCursor | None = None

    def register(self) -> None:
        registration = register(
            self._platform, self._retry, self._name, self._pull.target_bucket
        )
        lookback = timedelta(days=self._pull.initial_lookback_days)
        start = int((self._clock() - lookback).timestamp())
        self.collector_id = registration.id
        self.cursor = load_cursor(
            self._name, registration.cursor, PullCursor.parse
        ) or PullCursor(start)

    def _candidates(self, cursor: PullCursor) -> list[MispEventRef]:
        """Every event changed since the cursor, oldest first."""
        refs: set[MispEventRef] = set()
        page = 1
        while True:
            rows = self._retry(
                partial(
                    self._misp.index_events,
                    since=cursor.timestamp,
                    published_only=self._pull.published_only,
                    tags=self._pull.tags,
                    orgs=self._pull.orgs,
                    page=page,
                    limit=self._pull.page_size,
                ),
                "list MISP events",
            )
            refs.update(rows)
            if len(rows) < self._pull.page_size:
                break
            page += 1
        return sorted(
            (ref for ref in refs if not cursor.covers(self._position(ref), ref.uuid)),
            key=lambda ref: (self._position(ref), ref.uuid),
        )

    def _position(self, ref: MispEventRef) -> int:
        return index_position(ref, self._pull.published_only)

    def run_once(self) -> int:
        """Ingest every event changed since the cursor. Returns events ingested."""
        if self.collector_id is None or self.cursor is None:
            raise RuntimeError("The puller must register before pulling")
        collector_id = self.collector_id
        candidates = self._candidates(self.cursor)
        uuids = [ref.uuid for ref in candidates]
        records = self._retry(
            lambda: self._platform.lookup_misp_events(self._name, uuids),
            "look up sync records",
        )
        ingested = 0
        for ref in candidates:
            if self._stop.is_set():
                raise CollectorStopped
            record = records.get(ref.uuid)
            if record is not None and self._is_own_push(record, ref):
                self._advance(collector_id, ref)
                continue
            outcome = self._ingest(collector_id, ref)
            if outcome is _Outcome.REJECTED:
                return ingested
            if outcome is _Outcome.INGESTED:
                ingested += 1
            self._advance(collector_id, ref)
        if candidates:
            logger.info("[%s] pulled %d MISP events", self._name, ingested)
        return ingested

    def _is_own_push(self, record: MispEventRecord, ref: MispEventRef) -> bool:
        """Whether the event is STIXHub's own push coming back (not new intel)."""
        if record.origin is not MispEventOrigin.stixhub:
            return False
        if record.misp_timestamp == ref.timestamp:
            return True
        if record.misp_timestamp is None:
            # A push whose outcome never got recorded: the event is STIXHub's
            # if the organisation STIXHub pushes as created it (which may not
            # be this connector's own).
            event = self._retry(
                lambda: self._misp.get_event(ref.uuid), f"get event {ref.uuid}"
            )
            creator = record.creator_org or self._retry(
                self._misp.own_org_id, "get own organisation"
            )
            if event is not None and created_by(event, creator):
                self._retry(
                    lambda: self._platform.put_misp_event(
                        self._name,
                        ref.uuid,
                        MispEventOrigin.stixhub,
                        ref.timestamp,
                        creator,
                    ),
                    f"adopt event {ref.uuid}",
                )
                return True
        logger.info(
            "[%s] event %s was changed in MISP; MISP owns it now", self._name, ref.uuid
        )
        return False

    def _ingest(self, collector_id: int, ref: MispEventRef) -> "_Outcome":
        """Ingest one event."""
        event = self._retry(
            lambda: self._misp.get_event(ref.uuid), f"get event {ref.uuid}"
        )
        if event is None:
            logger.warning("[%s] event %s disappeared; skipping", self._name, ref.uuid)
            return _Outcome.SKIPPED
        try:
            objects = event_to_stix(event)
        except ConversionError:
            logger.exception("[%s] skipping event %s", self._name, ref.uuid)
            return _Outcome.SKIPPED
        status = self._retry(
            lambda: self._platform.submit_objects(collector_id, objects),
            f"submit event {ref.uuid}",
        )
        for failure in status.failures:
            logger.warning(
                "[%s] dropped object %s of event %s: %s",
                self._name,
                failure.id,
                ref.uuid,
                failure.message,
            )
        if status.success_count == 0:
            logger.error(
                "[%s] platform accepted none of the %d objects of event %s; "
                "retrying next cycle",
                self._name,
                len(objects),
                ref.uuid,
            )
            return _Outcome.REJECTED
        self._retry(
            lambda: self._platform.put_misp_event(
                self._name, ref.uuid, MispEventOrigin.misp, ref.timestamp
            ),
            f"record event {ref.uuid}",
        )
        return _Outcome.INGESTED

    def _advance(self, collector_id: int, ref: MispEventRef) -> None:
        assert self.cursor is not None
        cursor = self.cursor.past(self._position(ref), ref.uuid)
        self._retry(
            lambda: self._platform.update_cursor(collector_id, cursor.dump()),
            "update cursor",
        )
        self.cursor = cursor
