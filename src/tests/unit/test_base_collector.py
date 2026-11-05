import threading
from unittest.mock import MagicMock

import pytest

from collectors.base import BaseCollector, RegistrationError
from collectors.retry import CollectorStopped
from models.domain import TaxiiCollectorConfig


class _Collector(BaseCollector):
    def __init__(self, stop: threading.Event, error: BaseException | None) -> None:
        config = TaxiiCollectorConfig(
            name="c",
            secret_env="S",
            target_bucket="b",
            remote_url="http://x",
            remote_collection_id="id",
        )
        super().__init__(config, MagicMock(), stop_event=stop)
        self.error = error
        self.polls = 0

    def poll_once(self) -> int:
        self.polls += 1
        if self.error is not None:
            raise self.error
        return 0


def test_base_collector_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseCollector(MagicMock(), MagicMock())  # type: ignore[abstract]


def test_run_cycle_swallows_unexpected_errors() -> None:
    collector = _Collector(threading.Event(), ValueError("boom"))
    collector.run_cycle()
    assert collector.polls == 1


def test_run_cycle_propagates_stop() -> None:
    collector = _Collector(threading.Event(), CollectorStopped())
    with pytest.raises(CollectorStopped):
        collector.run_cycle()


def _registered(collector: _Collector, cursor: str | None = None) -> _Collector:
    platform = MagicMock()
    platform.register.return_value = MagicMock(id=7, cursor=cursor)
    collector._platform = platform
    return collector


def test_register_stores_id_and_cursor() -> None:
    collector = _registered(_Collector(threading.Event(), None), "cur")
    collector.register()
    assert collector.collector_id == 7
    assert collector.cursor == "cur"


def test_register_calls_cursor_hook_and_keeps_cursor_unset_on_failure() -> None:
    class _Strict(_Collector):
        def _check_cursor(self, cursor: str | None) -> None:
            raise RegistrationError("bad cursor")

    collector = _registered(_Strict(threading.Event(), None), "x")
    with pytest.raises(RegistrationError):
        collector.register()
    assert collector.cursor is None


def test_run_reports_registration_failure_to_on_fatal() -> None:
    class _Strict(_Collector):
        def _check_cursor(self, cursor: str | None) -> None:
            raise RegistrationError("bad cursor")

    collector = _registered(_Strict(threading.Event(), None))
    fatal: list[BaseException] = []
    collector.run(on_fatal=fatal.append)
    assert len(fatal) == 1
    assert collector.polls == 0


def test_run_polls_until_stopped() -> None:
    stop = threading.Event()
    collector = _registered(_Collector(stop, None))
    collector.config = collector.config.model_copy(update={"poll_interval": 0.01})

    original = collector.poll_once

    def poll() -> int:
        result = original()
        if collector.polls >= 3:
            stop.set()
        return result

    collector.poll_once = poll  # type: ignore[method-assign]
    collector.run()
    assert collector.polls == 3
