import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import certifi
import httpx
import pytest
from pydantic import SecretStr

from collectors.misp_collector import MispCollector
from collectors.platform_client import PlatformClient
from collectors.runner import CollectorRunner, create_runner
from collectors.taxii_client import TaxiiClient
from collectors.taxii_collector import TaxiiCollector
from models.domain import MispCollectorConfig, TaxiiCollectorConfig


def _config(name: str = "feed", shutdown_timeout: float = 5) -> TaxiiCollectorConfig:
    return TaxiiCollectorConfig(
        name=name,
        remote_url="https://taxii.example.com/api/",
        remote_collection_id="coll-1",
        target_bucket="intel",
        secret_env="FEED",
        poll_interval=3600,
        shutdown_timeout=shutdown_timeout,
    )


def _platform_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/collectors/register":
        return httpx.Response(200, json={"id": 1, "name": "feed", "bucket_id": 1})
    return httpx.Response(404)


def _collector(
    config: TaxiiCollectorConfig,
    stop: threading.Event,
    taxii_handler: httpx.MockTransport,
) -> TaxiiCollector:
    return TaxiiCollector(
        config,
        TaxiiClient(httpx.Client(transport=taxii_handler), config.remote_url, "c"),
        PlatformClient(
            httpx.Client(transport=httpx.MockTransport(_platform_handler)),
            "http://p",
            "secret",
        ),
        stop_event=stop,
        backoff_base=0,
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_runner_polls_then_stops_promptly() -> None:
    polled = threading.Event()

    def taxii(request: httpx.Request) -> httpx.Response:
        polled.set()
        return httpx.Response(200, json={"objects": [], "more": False})

    stop = threading.Event()
    runner = CollectorRunner(
        [_collector(_config(), stop, httpx.MockTransport(taxii))], stop
    )
    runner.start()
    assert polled.wait(5), "collector never polled"

    started = time.monotonic()
    runner.stop()

    # The thread was waiting out a 1h poll interval; the stop event wakes it.
    assert time.monotonic() - started < 2
    assert not any(t.is_alive() for t in runner.threads)


def test_runner_abandons_thread_after_shutdown_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = threading.Event()
    entered = threading.Event()

    def hanging_taxii(request: httpx.Request) -> httpx.Response:
        entered.set()
        release.wait(10)  # simulates a remote call stuck past shutdown
        return httpx.Response(200, json={"objects": []})

    stop = threading.Event()
    runner = CollectorRunner(
        [
            _collector(
                _config(shutdown_timeout=0.1), stop, httpx.MockTransport(hanging_taxii)
            )
        ],
        stop,
    )
    runner.start()
    assert entered.wait(5)

    with caplog.at_level(logging.WARNING, logger="collectors.runner"):
        runner.stop()

    assert runner.threads[0].is_alive()
    assert "did not stop within 0.1s" in caplog.text
    release.set()
    _wait_until(lambda: not runner.threads[0].is_alive())


def test_create_runner_fails_fast_on_missing_credentials() -> None:
    config = _config().model_copy(update={"credentials_env": "FEED"})
    with pytest.raises(RuntimeError, match="COLLECTOR_CREDENTIALS__FEED__USER"):
        create_runner(
            [config], "http://p", credentials={}, secrets={"feed": SecretStr("s")}
        )


def test_create_runner_fails_fast_on_missing_secret() -> None:
    with pytest.raises(RuntimeError, match="COLLECTOR_SECRETS__FEED"):
        create_runner([_config()], "http://p", secrets={})


def _misp_config(**kwargs: Any) -> MispCollectorConfig:
    return MispCollectorConfig(
        name="circl",
        type="misp",
        misp_url="https://misp.example.org",
        api_key_env="CIRCL",
        secret_env="CIRCL",
        target_bucket="intel",
        **kwargs,
    )


def test_create_runner_builds_misp_collector() -> None:
    runner = create_runner(
        [_misp_config(timeout=7, verify_tls=False)],
        "http://p",
        secrets={"circl": SecretStr("s")},
        api_keys={"circl": SecretStr("k")},
    )
    assert isinstance(runner._collectors[0], MispCollector)
    misp_http, platform_http = runner._clients["circl"]
    assert misp_http.timeout.read == 7
    assert platform_http.timeout.read == 30.0
    assert not misp_http._transport._pool._ssl_context.check_hostname  # type: ignore[attr-defined]


def test_create_runner_misp_uses_ca_bundle() -> None:
    cafile = certifi.where()
    runner = create_runner(
        [_misp_config(ca_bundle=cafile)],
        "http://p",
        secrets={"circl": SecretStr("s")},
        api_keys={"circl": SecretStr("k")},
    )
    misp_http = runner._clients["circl"][0]
    assert misp_http._transport._pool._ssl_context.check_hostname  # type: ignore[attr-defined]


def test_create_runner_fails_fast_on_missing_misp_api_key() -> None:
    with pytest.raises(RuntimeError, match="COLLECTOR_API_KEYS__CIRCL"):
        create_runner(
            [_misp_config()],
            "http://p",
            secrets={"circl": SecretStr("s")},
            api_keys={},
        )


def test_runner_stop_closes_misp_clients() -> None:
    runner = create_runner(
        [_misp_config()],
        "http://p",
        secrets={"circl": SecretStr("s")},
        api_keys={"circl": SecretStr("k")},
    )
    runner._collectors[0].run = lambda on_fatal=None: None  # type: ignore[method-assign]
    runner.start()
    runner.stop()
    assert all(c.is_closed for c in runner._clients["circl"])
