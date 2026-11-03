import logging
import os
import signal
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

import httpx

from collectors.misp.client import PyMispClient
from collectors.misp.connector import MispConnector
from collectors.platform_client import PlatformClient
from collectors.taxii_client import TaxiiClient
from collectors.taxii_collector import TaxiiCollector
from collectors.validation import load_credentials, load_misp_api_key
from config import CollectorCredentials, MispCredentials
from models.domain import CollectorConfig, MispConnectorConfig, TaxiiCollectorConfig

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


class Closeable(Protocol):
    def close(self) -> None: ...


class RunnableConfig(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def shutdown_timeout(self) -> float: ...


class Collector(Protocol):
    """Anything the runner can run on a thread: a TAXII collector, a MISP connector."""

    @property
    def config(self) -> RunnableConfig: ...

    def run(self, on_fatal: FatalHandler | None = None) -> None: ...


class CollectorRunner:
    """Runs each collector on its own daemon thread and stops them on shutdown."""

    def __init__(
        self,
        collectors: Sequence[Collector],
        stop_event: threading.Event,
        on_fatal: FatalHandler | None = None,
        clients: Mapping[str, Sequence[Closeable]] | None = None,
    ) -> None:
        self._collectors = collectors
        self._stop_event = stop_event
        self._on_fatal = on_fatal
        self._clients = clients or {}
        self._threads: list[tuple[Collector, threading.Thread]] = []

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


def _taxii_collector(
    config: TaxiiCollectorConfig,
    platform_url: str,
    stop_event: threading.Event,
    credentials: Mapping[str, CollectorCredentials] | None,
) -> tuple[Collector, list[Closeable]]:
    auth = load_credentials(config, credentials)
    taxii_http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    platform_http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    collector = TaxiiCollector(
        config,
        TaxiiClient(taxii_http, config.remote_url, config.remote_collection_id, auth),
        PlatformClient(platform_http, platform_url),
        stop_event=stop_event,
    )
    return collector, [taxii_http, platform_http]


def _misp_connector(
    config: MispConnectorConfig,
    all_configs: Sequence[CollectorConfig],
    platform_url: str,
    stop_event: threading.Event,
    credentials: Mapping[str, MispCredentials] | None,
) -> tuple[Collector, list[Closeable]]:
    api_key = load_misp_api_key(config, credentials)
    misp = PyMispClient(config.url, api_key, config.verify_tls, HTTP_TIMEOUT_SECONDS)
    platform_http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    # Intel pulled from the same MISP instance by another connector must not be
    # pushed back to it either.
    same_instance = frozenset(
        f"collector:{other.name}"
        for other in all_configs
        if isinstance(other, MispConnectorConfig)
        and other.normalized_url == config.normalized_url
    )
    connector = MispConnector(
        config,
        misp,
        PlatformClient(platform_http, platform_url),
        excluded_creators=same_instance,
        stop_event=stop_event,
    )
    return connector, [misp, platform_http]


def create_runner(
    configs: Sequence[CollectorConfig],
    platform_url: str,
    on_fatal: FatalHandler | None = terminate_process,
    credentials: Mapping[str, CollectorCredentials] | None = None,
    misp_credentials: Mapping[str, MispCredentials] | None = None,
) -> CollectorRunner:
    """Build collectors from config. Raises if credentials are misconfigured.

    ``credentials`` and ``misp_credentials`` default to
    ``settings.COLLECTOR_CREDENTIALS`` and ``settings.MISP_CREDENTIALS``.
    """
    stop_event = threading.Event()
    collectors: list[Collector] = []
    clients: dict[str, list[Closeable]] = {}
    for config in configs:
        if isinstance(config, MispConnectorConfig):
            collector, closeables = _misp_connector(
                config, configs, platform_url, stop_event, misp_credentials
            )
        else:
            collector, closeables = _taxii_collector(
                config, platform_url, stop_event, credentials
            )
        collectors.append(collector)
        clients[config.name] = closeables
    return CollectorRunner(collectors, stop_event, on_fatal, clients)
