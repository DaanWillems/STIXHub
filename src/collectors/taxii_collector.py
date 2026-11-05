import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from collectors.base import BaseCollector, _utcnow
from collectors.platform_client import PlatformClient
from collectors.retry import CollectorStopped
from collectors.taxii_client import TaxiiClient, TaxiiPage
from models.domain import TaxiiCollectorConfig

logger = logging.getLogger(__name__)


def taxii_timestamp(moment: datetime) -> str:
    """Format a datetime as a TAXII 2.1 / STIX timestamp (UTC, ``Z`` suffix)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TaxiiCollector(BaseCollector):
    """Polls one remote TAXII 2.1 collection and pushes objects to the platform.

    Entirely synchronous; designed to run on its own thread (see ``run``).
    """

    def __init__(
        self,
        config: TaxiiCollectorConfig,
        taxii: TaxiiClient,
        platform: PlatformClient,
        stop_event: threading.Event | None = None,
        backoff_base: float = 1.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        super().__init__(config, platform, stop_event, backoff_base, clock)
        self.config: TaxiiCollectorConfig = config
        self._taxii = taxii

    def poll_once(self) -> int:
        """Drain all pages added after the cursor. Returns objects accepted.

        Raises ``RetriesExhausted`` / ``httpx.HTTPStatusError`` when a step
        fails; the cursor only reflects batches the platform accepted.
        """
        if self.collector_id is None:
            raise RuntimeError("Collector must register before polling")
        collector_id = self.collector_id
        attempts = self.config.max_retries
        # All pages of one cycle belong to the same query, so added_after is
        # fixed for the cycle even while the cursor advances page by page.
        added_after = self.cursor
        next_token: str | None = None
        accepted = 0

        while True:
            token = next_token
            page: TaxiiPage = self._retry(
                lambda: self._taxii.get_objects(added_after, token),
                "fetch TAXII page",
                attempts,
            )

            if page.objects:
                status = self._retry(
                    lambda: self._platform.submit_objects(collector_id, page.objects),
                    "submit objects",
                    attempts,
                )
                for failure in status.failures:
                    logger.warning(
                        "[%s] dropped object %s: %s",
                        self.config.name,
                        failure.id,
                        failure.message,
                    )
                accepted += status.success_count
                if status.success_count > 0:
                    new_cursor = page.date_added_last or taxii_timestamp(self._clock())
                    self._retry(
                        lambda: self._platform.update_cursor(collector_id, new_cursor),
                        "update cursor",
                        attempts,
                    )
                    self.cursor = new_cursor
                else:
                    logger.error(
                        "[%s] platform accepted none of %d objects; "
                        "cursor not advanced",
                        self.config.name,
                        len(page.objects),
                    )

            if not page.more:
                break
            if not page.next:
                logger.warning(
                    "[%s] TAXII server reported more=true without a next token; "
                    "stopping this poll cycle",
                    self.config.name,
                )
                break
            if self._stop.is_set():
                raise CollectorStopped
            next_token = page.next

        logger.info(
            "[%s] poll complete: %d objects accepted, cursor=%s",
            self.config.name,
            accepted,
            self.cursor,
        )
        return accepted
