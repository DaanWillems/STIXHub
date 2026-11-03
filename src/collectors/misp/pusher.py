"""Push: reports and groupings in the push bucket -> MISP events."""

import copy
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from enum import Enum
from functools import partial
from typing import Any

from collector_auth import push_registration
from collectors.misp.client import (
    MispApi,
    MispRequestError,
    MispUnavailable,
    created_by,
    event_timestamp,
)
from collectors.misp.common import Retrier, load_cursor, register
from collectors.misp.convert import ConversionError, stix_to_event
from collectors.platform_client import PlatformClient
from collectors.retry import CollectorStopped
from models.domain import (
    CollectorEntity,
    MispConnectorConfig,
    MispEventOrigin,
    MispEventRecord,
    MispPushConfig,
)
from processors.stix import TLP_VALUES_BY_ID

logger = logging.getLogger(__name__)

CONTAINER_TYPES = frozenset({"report", "grouping"})


class _Outcome(Enum):
    WRITTEN = "written"
    SKIPPED = "skipped"
    # Objects it references haven't reached the bucket yet: try again later.
    DEFERRED = "deferred"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# The platform grants this registration read access to the push bucket.
push_name = push_registration


def object_refs(obj: dict[str, Any]) -> Iterator[str]:
    """Every STIX ID in a top-level ``*_ref`` / ``*_refs`` property."""
    for key, value in obj.items():
        if key.endswith("_ref") and isinstance(value, str):
            yield value
        elif key.endswith("_refs") and isinstance(value, list):
            yield from (ref for ref in value if isinstance(ref, str))


def original_id(entity: CollectorEntity) -> str:
    """The ID the object had at its source (its platform ID if it has none)."""
    return entity.other_stix_ids[0] if entity.other_stix_ids else entity.stix_id


def restore_original_ids(entities: list[CollectorEntity]) -> list[dict[str, Any]]:
    """The entities' objects under their original IDs, refs rewritten to match.

    misp-stix derives MISP UUIDs from STIX IDs, so this is what makes an event
    keep its attribute and object UUIDs through STIXHub.
    """
    originals: dict[str, str] = {}
    for entity in entities:
        originals.setdefault(entity.stix_id, original_id(entity))

    def rewrite(value: Any) -> Any:
        if isinstance(value, str):
            return originals.get(value, value)
        return [
            originals.get(ref, ref) if isinstance(ref, str) else ref for ref in value
        ]

    objects: dict[str, dict[str, Any]] = {}
    for entity in entities:
        obj = copy.deepcopy(entity.object)
        obj["id"] = original_id(entity)
        for key in list(obj):
            if key.endswith(("_ref", "_refs")):
                obj[key] = rewrite(obj[key])
        objects.setdefault(obj["id"], obj)
    return list(objects.values())


class MispPusher:
    def __init__(
        self,
        config: MispConnectorConfig,
        misp: MispApi,
        platform: PlatformClient,
        retry: Retrier,
        stop: threading.Event,
        excluded_creators: frozenset[str],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        assert config.push is not None
        self._name = config.name
        self._push: MispPushConfig = config.push
        self._misp = misp
        self._platform = platform
        self._retry = retry
        self._stop = stop
        self._excluded = excluded_creators | set(config.push.exclude_creators)
        self._clock = clock
        self.collector_id: int | None = None
        self.cursor = 0

    def register(self) -> None:
        registration = register(
            self._platform,
            self._retry,
            push_name(self._name),
            self._push.source_bucket,
        )
        self.collector_id = registration.id
        self.cursor = load_cursor(push_name(self._name), registration.cursor, int) or 0

    def run_once(self) -> int:
        """Push every container added since the cursor. Returns events written."""
        if self.collector_id is None:
            raise RuntimeError("The pusher must register before pushing")
        collector_id = self.collector_id
        pushed = 0
        while True:
            page = self._retry(
                partial(
                    self._platform.list_entities,
                    collector_id,
                    self.cursor,
                    self._push.batch_size,
                    self._push.settle_seconds,
                ),
                "list entities",
            )
            if not page:
                break
            # Only the newest version of a container in this page matters.
            newest = {
                entity.stix_id: entity
                for entity in page
                if entity.type in CONTAINER_TYPES
                and entity.creator not in self._excluded
            }
            for container in sorted(newest.values(), key=lambda e: e.id):
                if self._stop.is_set():
                    raise CollectorStopped
                try:
                    outcome = self._push_container(collector_id, container)
                except MispRequestError as exc:
                    logger.error(
                        "[%s] MISP refused %s: %s",
                        self._name,
                        original_id(container),
                        exc,
                    )
                    outcome = _Outcome.SKIPPED
                if outcome is _Outcome.DEFERRED:
                    # Resume at this container next cycle; nothing after it
                    # is pushed before it.
                    self._save_cursor(collector_id, container.id - 1)
                    return pushed
                if outcome is _Outcome.WRITTEN:
                    pushed += 1
            self._save_cursor(collector_id, page[-1].id)
            if len(page) < self._push.batch_size:
                break
        if pushed:
            logger.info("[%s] pushed %d MISP events", self._name, pushed)
        return pushed

    def _save_cursor(self, collector_id: int, cursor: int) -> None:
        if cursor == self.cursor:
            return
        self._retry(
            partial(self._platform.update_cursor, collector_id, str(cursor)),
            "update cursor",
        )
        self.cursor = cursor

    def _collect(
        self, collector_id: int, container: CollectorEntity
    ) -> tuple[list[CollectorEntity], set[str]]:
        """The container and everything it references, directly or not, and
        the refs that aren't in the bucket."""
        collected = {container.id: container}
        missing: set[str] = set()
        known = {container.stix_id, *container.other_stix_ids}
        frontier = set(object_refs(container.object)) - known
        while frontier:
            wanted = sorted(frontier)
            found = self._retry(
                partial(self._platform.lookup_entities, collector_id, wanted),
                "look up referenced objects",
            )
            missing.update(ref for ref in wanted if ref not in found)
            known.update(wanted)
            frontier = set()
            for entity in found.values():
                if entity.id in collected:
                    continue
                collected[entity.id] = entity
                known.update({entity.stix_id, *entity.other_stix_ids})
                frontier.update(object_refs(entity.object))
            frontier -= known
        return list(collected.values()), missing

    def _push_container(
        self, collector_id: int, container: CollectorEntity
    ) -> "_Outcome":
        """Create or update the container's event."""
        event_uuid = original_id(container).split("--", 1)[-1]
        entities, missing = self._collect(collector_id, container)
        # Well-known TLP markings are often referenced without being sent.
        missing -= TLP_VALUES_BY_ID.keys()
        if missing:
            waited = (self._clock() - container.platform_created).total_seconds()
            if waited < self._push.wait_for_refs_seconds:
                logger.info(
                    "[%s] %s waits for %d referenced objects",
                    self._name,
                    original_id(container),
                    len(missing),
                )
                return _Outcome.DEFERRED
            logger.warning(
                "[%s] pushing %s without %d objects missing from bucket '%s': %s",
                self._name,
                original_id(container),
                len(missing),
                self._push.source_bucket,
                ", ".join(sorted(missing)),
            )

        records = self._retry(
            lambda: self._platform.lookup_misp_events(self._name, [event_uuid]),
            "look up sync record",
        )
        record = records.get(event_uuid)
        if record is not None and record.origin is MispEventOrigin.misp:
            return _Outcome.SKIPPED
        remote = self._retry(
            lambda: self._misp.get_event(event_uuid), f"get event {event_uuid}"
        )
        if not self._may_write(record, remote, event_uuid):
            return _Outcome.SKIPPED

        try:
            event = stix_to_event(
                restore_original_ids(entities),
                event_uuid=event_uuid,
                distribution=self._push.distribution,
                sharing_group_id=self._push.sharing_group_id,
            )
        except ConversionError:
            logger.exception("[%s] skipping %s", self._name, original_id(container))
            return _Outcome.SKIPPED

        try:
            if remote is None:
                timestamp = self._add(event_uuid, event)
                if timestamp is None:
                    return _Outcome.SKIPPED
            else:
                timestamp = self._retry(
                    lambda: self._misp.update_event(event_uuid, event),
                    f"update event {event_uuid}",
                )
            self._record(event_uuid, timestamp)
            if self._push.publish:
                self._retry(
                    lambda: self._misp.publish_event(event_uuid),
                    f"publish event {event_uuid}",
                )
                # Record the event as published, whatever publishing did to
                # its timestamp, so it isn't mistaken for an edit in MISP.
                published = self._retry(
                    lambda: self._misp.get_event(event_uuid), f"get event {event_uuid}"
                )
                if published is not None:
                    self._record(event_uuid, event_timestamp(published))
        except MispRequestError as exc:
            logger.error("[%s] MISP refused event %s: %s", self._name, event_uuid, exc)
            return _Outcome.SKIPPED
        return _Outcome.WRITTEN

    def _add(self, event_uuid: str, event: dict[str, Any]) -> int | None:
        """Create the event; its timestamp, or ``None`` if STIXHub doesn't own it.

        Adding isn't idempotent, so it is never retried blindly: after a
        failure, whether MISP created the event decides what happens next.
        """
        self._record(event_uuid, None)  # pending: survives a crash mid-add
        try:
            return self._misp.add_event(event)
        except (MispUnavailable, MispRequestError) as exc:
            failure: MispUnavailable | MispRequestError = exc
        remote = self._retry(
            lambda: self._misp.get_event(event_uuid), f"get event {event_uuid}"
        )
        if remote is None:
            # Nothing was created: a transient failure is retried next cycle,
            # a refusal is final.
            raise failure
        if self._is_ours(remote):
            logger.info(
                "[%s] adding event %s failed (%s) but MISP created it; adopting it",
                self._name,
                event_uuid,
                failure,
            )
            return event_timestamp(remote)
        self._hand_to_misp(event_uuid, event_timestamp(remote))
        return None

    def _is_ours(self, event: dict[str, Any]) -> bool:
        org_id = self._retry(self._misp.own_org_id, "get own organisation")
        return created_by(event, org_id)

    def _hand_to_misp(self, event_uuid: str, timestamp: int) -> None:
        logger.warning(
            "[%s] event %s belongs to someone else in MISP; STIXHub won't write it",
            self._name,
            event_uuid,
        )
        self._retry(
            lambda: self._platform.put_misp_event(
                self._name, event_uuid, MispEventOrigin.misp, timestamp
            ),
            f"hand event {event_uuid} to MISP",
        )

    def _may_write(
        self,
        record: MispEventRecord | None,
        remote: dict[str, Any] | None,
        event_uuid: str,
    ) -> bool:
        """Apply the ownership rules (see specs/misp-connector.md, Push)."""
        if record is None:
            if remote is not None:
                logger.info(
                    "[%s] event %s already exists in MISP and STIXHub didn't "
                    "create it; skipping",
                    self._name,
                    event_uuid,
                )
                return False
            return True
        if remote is None:
            if record.misp_timestamp is None:
                return True  # an earlier add never reached MISP: retry it
            logger.info(
                "[%s] event %s was deleted in MISP; not recreating it",
                self._name,
                event_uuid,
            )
            return False
        remote_timestamp = event_timestamp(remote)
        if record.misp_timestamp is None:
            # An earlier add may have reached MISP without being recorded:
            # adopt the event if STIXHub's organisation created it.
            if not self._is_ours(remote):
                self._hand_to_misp(event_uuid, remote_timestamp)
                return False
            self._record(event_uuid, remote_timestamp)
            return True
        if record.misp_timestamp != remote_timestamp:
            logger.warning(
                "[%s] event %s was edited in MISP; MISP owns it now and STIXHub "
                "won't update it",
                self._name,
                event_uuid,
            )
            self._retry(
                lambda: self._platform.put_misp_event(
                    self._name, event_uuid, MispEventOrigin.misp, remote_timestamp
                ),
                f"hand event {event_uuid} to MISP",
            )
            return False
        return True

    def _record(self, event_uuid: str, timestamp: int | None) -> None:
        self._retry(
            lambda: self._platform.put_misp_event(
                self._name, event_uuid, MispEventOrigin.stixhub, timestamp
            ),
            f"record event {event_uuid}",
        )
