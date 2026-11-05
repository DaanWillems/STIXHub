import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

import httpx

from collectors.platform_client import PlatformClient
from collectors.retry import CollectorStopped, RetriesExhausted, with_retries
from models.domain import BaseCollectorConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Registration happens while the HTTP server may still be starting, so it gets
# a more generous retry budget than a regular poll step.
REGISTRATION_ATTEMPTS = 10


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RegistrationError(RuntimeError):
    """The platform refused or could not complete collector registration."""


class BaseCollector(ABC):
    """Registration, run loop and error handling shared by all collectors.

    Entirely synchronous; designed to run on its own thread (see ``run``).
    Subclasses implement ``poll_once``.
    """

    def __init__(
        self,
        config: BaseCollectorConfig,
        platform: PlatformClient,
        stop_event: threading.Event | None = None,
        backoff_base: float = 1.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.config = config
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

    @abstractmethod
    def poll_once(self) -> int:
        """Run one poll cycle. Returns the number of objects accepted.

        Raises ``RetriesExhausted`` / ``httpx.HTTPStatusError`` when a step
        fails; the cursor only reflects batches the platform accepted.
        """

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
