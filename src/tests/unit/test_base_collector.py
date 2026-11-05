import threading
from unittest.mock import MagicMock

import pytest

from collectors.base import BaseCollector
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
