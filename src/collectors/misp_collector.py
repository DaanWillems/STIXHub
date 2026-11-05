import functools
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from collectors.base import BaseCollector, RegistrationError, utcnow
from collectors.misp_client import MispClient, MispEventSummary
from collectors.misp_convert import chunk, convert_event
from collectors.misp_cursor import MispCursor, MispCursorError
from collectors.platform_client import PlatformClient
from collectors.retry import CollectorStopped
from models.domain import MispCollectorConfig

logger = logging.getLogger(__name__)


class MispCollector(BaseCollector):
    """Polls one MISP instance for published events and pushes them as STIX.

    Phase 1 lists published events in a frozen publish-timestamp window; phase 2
    fetches, converts and submits each event, advancing the cursor per event.
    """

    def __init__(
        self,
        config: MispCollectorConfig,
        misp: MispClient,
        platform: PlatformClient,
        stop_event: threading.Event | None = None,
        backoff_base: float = 1.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(config, platform, stop_event, backoff_base, clock)
        self.config: MispCollectorConfig = config
        self._misp = misp

    def _check_cursor(self, cursor: str | None) -> None:
        if cursor is None:
            return
        try:
            MispCursor.parse(cursor)
        except MispCursorError as exc:
            raise RegistrationError(
                f"Collector '{self.config.name}' has a stored cursor that is not a "
                f"MISP cursor ({exc}). If the collector type was changed, reset the "
                f"stored cursor for this collector and restart."
            ) from exc

    def _save_cursor(self, collector_id: int, cursor: MispCursor) -> None:
        raw = cursor.serialize()
        self._retry(
            lambda: self._platform.update_cursor(collector_id, raw),
            "update cursor",
            self.config.max_retries,
        )
        self.cursor = raw

    def poll_once(self) -> int:
        """Run one poll. Returns the number of objects accepted by the platform."""
        if self.collector_id is None:
            raise RuntimeError("Collector must register before polling")
        collector_id = self.collector_id
        cfg = self.config
        attempts = cfg.max_retries
        now = int(self._clock().timestamp())

        first_run = self.cursor is None
        if self.cursor is None:
            cursor = MispCursor.initial(
                now, cfg.initial_lookback_days, cfg.overlap_seconds
            )
        else:
            cursor = MispCursor.parse(self.cursor)
        window_start = cursor.window_start(cfg.overlap_seconds)

        try:
            events = self._retry(
                lambda: self._misp.list_published(
                    window_start,
                    now,
                    tags=cfg.tags,
                    org=cfg.org,
                    page_size=cfg.page_size,
                    should_stop=self._stop.is_set,
                ),
                "list MISP events",
                attempts,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                logger.error(
                    "[%s] MISP rejected the API key (HTTP %d) while listing events; "
                    "aborting poll",
                    cfg.name,
                    exc.response.status_code,
                )
                return 0
            raise

        if first_run:
            # Fix the lookback window once, even if there is nothing to ingest.
            self._save_cursor(collector_id, cursor)

        accepted = 0
        for summary in events:
            if self._stop.is_set():
                raise CollectorStopped
            if cursor.should_skip(summary.uuid, summary.publish_timestamp):
                continue
            accepted += self._handle_event(collector_id, summary)
            cursor = cursor.advance(
                summary.uuid, summary.publish_timestamp, cfg.overlap_seconds
            )
            self._save_cursor(collector_id, cursor)

        logger.info(
            "[%s] poll complete: %d events listed, %d objects accepted",
            cfg.name,
            len(events),
            accepted,
        )
        return accepted

    def _handle_event(self, collector_id: int, summary: MispEventSummary) -> int:
        """Fetch, convert and submit one event. Returns objects accepted.

        Events that cannot be processed deterministically are logged and
        skipped (the caller still advances the cursor). Transient failures
        propagate and leave the cursor untouched.
        """
        name = self.config.name
        attempts = self.config.max_retries
        try:
            event = self._retry(
                lambda: self._misp.get_event(summary.uuid), "fetch MISP event", attempts
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 404):
                logger.warning(
                    "[%s] event %s not available (HTTP %d); skipping",
                    name,
                    summary.uuid,
                    exc.response.status_code,
                )
                return 0
            raise

        if str(event.get("uuid")) != summary.uuid or not _is_published(event):
            logger.warning(
                "[%s] event %s changed or was unpublished since listing; skipping",
                name,
                summary.uuid,
            )
            return 0

        try:
            objects = convert_event(event)
        except Exception:
            logger.exception(
                "[%s] conversion of event %s failed; skipping", name, summary.uuid
            )
            return 0
        if not objects:
            logger.warning(
                "[%s] event %s produced no STIX objects; skipping", name, summary.uuid
            )
            return 0

        accepted = 0
        for batch in chunk(objects, self.config.submit_batch_size):
            try:
                status = self._retry(
                    functools.partial(
                        self._platform.submit_objects, collector_id, batch
                    ),
                    "submit objects",
                    attempts,
                )
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "[%s] platform rejected a chunk of event %s (HTTP %d: %s); "
                    "skipping event",
                    name,
                    summary.uuid,
                    exc.response.status_code,
                    exc.response.text,
                )
                return accepted
            for failure in status.failures:
                logger.warning(
                    "[%s] dropped object %s of event %s: %s",
                    name,
                    failure.id,
                    summary.uuid,
                    failure.message,
                )
            accepted += status.success_count
        return accepted


def _is_published(event: dict[str, Any]) -> bool:
    published = event.get("published")
    return published is True or published in ("1", 1)
