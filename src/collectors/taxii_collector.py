import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

import httpx

from collectors.platform_client import PlatformClient
from collectors.retry import CollectorStopped, RetriesExhausted, with_retries
from collectors.taxii_client import TaxiiClient, TaxiiPage
from models.domain import CollectorConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Registration happens while the HTTP server may still be starting, so it gets
# a more generous retry budget than a regular poll step.
REGISTRATION_ATTEMPTS = 10


def taxii_timestamp(moment: datetime) -> str:
    """Format a datetime as a TAXII 2.1 / STIX timestamp (UTC, ``Z`` suffix)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RegistrationError(RuntimeError):
    """The platform refused or could not complete collector registration."""


class TaxiiCollector:
    """Polls one remote TAXII 2.1 collection and pushes objects to the platform.

    Entirely synchronous; designed to run on its own thread (see ``run``).
    """

    def __init__(
        self,
        config: CollectorConfig,
        taxii: TaxiiClient,
        platform: PlatformClient,
        stop_event: threading.Event | None = None,
        backoff_base: float = 1.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.config = config
        self._taxii = taxii
        self._platform = platform
        self._stop = stop_event or threading.Event()
        self._backoff_base = backoff_base
        self._clock = clock
        self.collector_id: int | None = None
        self.cursor: str | None = None

    # -- helpers ---------------------------------------------------------

    def _retry(self, fn: Callable[[], T], description: str, attempts: int) -> T:
        return with_retries(
            fn,
            description=f"[{self.config.name}] {description}",
            max_retries=attempts,
            wait=self._stop.wait,
            base_delay=self._backoff_base,
        )

    # -- lifecycle ---------------------------------------------------------

    def register(self) -> None:
        """Register with the platform and load the stored cursor."""
        try:
            registration = self._retry(
                lambda: self._platform.register(
                    self.config.name, self.config.target_bucket
                ),
                "registration",
                REGISTRATION_ATTEMPTS,
            )
        except (RetriesExhausted, httpx.HTTPError) as exc:
            detail = (
                f"HTTP {exc.response.status_code} {exc.response.text}"
                if isinstance(exc, httpx.HTTPStatusError)
                else str(exc)
            )
            raise RegistrationError(
                f"Collector '{self.config.name}' could not register with the "
                f"platform (target bucket '{self.config.target_bucket}'): {detail}"
            ) from exc
        self.collector_id = registration.id
        self.cursor = registration.cursor
        logger.info(
            "[%s] registered as collector %s, cursor=%s",
            self.config.name,
            self.collector_id,
            self.cursor,
        )

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

    def run_cycle(self) -> None:
        """Run one poll cycle, logging (not raising) failures."""
        try:
            self.poll_once()
        except RetriesExhausted as exc:
            logger.error("%s; waiting for next poll", exc)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[%s] permanent failure (HTTP %d from %s); not retrying until "
                "next poll",
                self.config.name,
                exc.response.status_code,
                exc.request.url,
            )
        except CollectorStopped:
            raise
        except Exception:
            logger.exception("[%s] unexpected error during poll", self.config.name)

    def run(self, on_fatal: Callable[[BaseException], None] | None = None) -> None:
        """Thread entry point: register, then poll until the stop event is set."""
        try:
            self.register()
        except CollectorStopped:
            return
        except RegistrationError as exc:
            logger.critical("%s", exc)
            if on_fatal is not None:
                on_fatal(exc)
            return

        try:
            while not self._stop.is_set():
                self.run_cycle()
                if self._stop.wait(self.config.poll_interval):
                    break
        except CollectorStopped:
            pass
        logger.info("[%s] stopped", self.config.name)
