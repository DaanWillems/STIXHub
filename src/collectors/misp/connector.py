"""A MISP connector: pull and/or push on one thread (see specs/misp-connector.md)."""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from collectors.misp.client import MispApi, MispRequestError, MispUnavailable
from collectors.misp.common import RegistrationError, Retrier
from collectors.misp.puller import MispPuller
from collectors.misp.pusher import MispPusher
from collectors.platform_client import PlatformClient
from collectors.retry import CollectorStopped, RetriesExhausted
from models.domain import MispConnectorConfig

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MispConnector:
    def __init__(
        self,
        config: MispConnectorConfig,
        misp: MispApi,
        platform: PlatformClient,
        excluded_creators: frozenset[str] = frozenset(),
        stop_event: threading.Event | None = None,
        backoff_base: float = 1.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.config = config
        self._stop = stop_event or threading.Event()
        retry = Retrier(config.name, config.max_retries, self._stop, backoff_base)
        self.puller = (
            MispPuller(config, misp, platform, retry, self._stop, clock)
            if config.pull is not None
            else None
        )
        # Never push back what was pulled from this MISP instance.
        own = frozenset({f"collector:{config.name}"})
        self.pusher = (
            MispPusher(
                config,
                misp,
                platform,
                retry,
                self._stop,
                own | excluded_creators,
                clock,
            )
            if config.push is not None
            else None
        )

    def register(self) -> None:
        for step in (self.puller, self.pusher):
            if step is not None:
                step.register()

    def run_cycle(self) -> None:
        """Pull, then push. A failing step is logged and doesn't stop the other."""
        for label, step in (("pull", self.puller), ("push", self.pusher)):
            if step is None:
                continue
            try:
                step.run_once()
            except CollectorStopped:
                raise
            except (
                RetriesExhausted,
                MispRequestError,
                MispUnavailable,
                httpx.HTTPStatusError,
            ) as exc:
                logger.error(
                    "[%s] %s failed: %s; retrying next cycle",
                    self.config.name,
                    label,
                    exc,
                )
            except Exception:
                logger.exception(
                    "[%s] unexpected error during %s", self.config.name, label
                )

    def run(self, on_fatal: Callable[[BaseException], None] | None = None) -> None:
        """Thread entry point: register, then sync until the stop event is set."""
        try:
            self.register()
        except CollectorStopped:
            return
        except RegistrationError as exc:
            logger.critical("MISP connector '%s': %s", self.config.name, exc)
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
