import logging
import os
import signal
import threading
from collections.abc import Callable, Mapping

import httpx
from pydantic import SecretStr

from collectors.platform_client import PlatformClient
from collectors.taxii_client import TaxiiClient
from collectors.taxii_collector import TaxiiCollector
from collectors.validation import load_credentials, load_secret
from config import CollectorCredentials
from models.domain import CollectorConfig

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 30.0

FatalHandler = Callable[[BaseException], None]


def terminate_process(exc: BaseException) -> None:
    """Shut the application down after a fatal collector error.

    Registration runs on a collector thread once the HTTP server is up, so it
    cannot raise into the lifespan; SIGTERM triggers uvicorn's normal shutdown.
    """
    logger.critical("Fatal collector error, shutting down: %s", exc)
    os.kill(os.getpid(), signal.SIGTERM)


class CollectorRunner:
    """Runs each collector on its own daemon thread and stops them on shutdown."""

    def __init__(
        self,
        collectors: list[TaxiiCollector],
        stop_event: threading.Event,
        on_fatal: FatalHandler | None = None,
        clients: Mapping[str, list[httpx.Client]] | None = None,
    ) -> None:
        self._collectors = collectors
        self._stop_event = stop_event
        self._on_fatal = on_fatal
        self._clients = clients or {}
        self._threads: list[tuple[TaxiiCollector, threading.Thread]] = []

    @property
    def threads(self) -> list[threading.Thread]:
        return [thread for _, thread in self._threads]

    def start(self) -> None:
        for collector in self._collectors:
            thread = threading.Thread(
                target=collector.run,
                kwargs={"on_fatal": self._on_fatal},
                name=f"collector-{collector.config.name}",
                daemon=True,
            )
            thread.start()
            self._threads.append((collector, thread))
            logger.info("Started collector '%s'", collector.config.name)

    def stop(self) -> None:
        """Signal all collectors and wait up to each one's ``shutdown_timeout``."""
        self._stop_event.set()
        for collector, thread in self._threads:
            name = collector.config.name
            thread.join(timeout=collector.config.shutdown_timeout)
            if thread.is_alive():
                logger.warning(
                    "Collector '%s' did not stop within %ss; abandoning it",
                    name,
                    collector.config.shutdown_timeout,
                )
                continue
            for client in self._clients.get(name, []):
                client.close()


def create_runner(
    configs: list[CollectorConfig],
    platform_url: str,
    on_fatal: FatalHandler | None = terminate_process,
    credentials: Mapping[str, CollectorCredentials] | None = None,
    secrets: Mapping[str, SecretStr] | None = None,
) -> CollectorRunner:
    """Build collectors from config. Raises if credentials or secrets are missing.

    ``credentials`` defaults to ``settings.COLLECTOR_CREDENTIALS`` and
    ``secrets`` to ``settings.COLLECTOR_SECRETS``.
    """
    stop_event = threading.Event()
    collectors: list[TaxiiCollector] = []
    clients: dict[str, list[httpx.Client]] = {}
    for config in configs:
        auth = load_credentials(config, credentials)
        secret = load_secret(config, secrets)
        taxii_http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        platform_http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        clients[config.name] = [taxii_http, platform_http]
        collectors.append(
            TaxiiCollector(
                config,
                TaxiiClient(
                    taxii_http, config.remote_url, config.remote_collection_id, auth
                ),
                PlatformClient(platform_http, platform_url, secret),
                stop_event=stop_event,
            )
        )
    return CollectorRunner(collectors, stop_event, on_fatal, clients)
