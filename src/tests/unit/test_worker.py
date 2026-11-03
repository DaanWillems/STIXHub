import asyncio
import contextlib
import copy
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from database.repositories.bucket import InMemoryBucketRepository
from models.domain import Bucket, PlatformConfig, ProcessingStatus, StixEntity
from pipeline.pipeline import PipelineValidationError
from pipeline.pipeline_manager import PipelineManager
from worker.__main__ import SinkNotFoundError, Worker

ADD_LABEL = {"action": "add", "field": "labels", "value": "refined"}


def _config(
    pipelines: list[dict[str, Any]],
    buckets: tuple[str, ...] = ("raw", "refined", "shared"),
) -> PlatformConfig:
    return PlatformConfig.model_validate(
        {
            "buckets": [{"name": b, "mode": "append"} for b in buckets],
            "collections": [],
            "roles": [
                {"name": "refiner", "can_read": list(buckets), "can_write": list(buckets)}
            ],
            "pipelines": pipelines,
        }
    )


def _pipeline(
    sources: list[str], sinks: list[str], name: str = "refine"
) -> dict[str, Any]:
    return {
        "name": name,
        "role": "refiner",
        "sources": sources,
        "sinks": sinks,
        "steps": [ADD_LABEL],
    }


def _indicator(bucket_id: int, n: int) -> StixEntity:
    stix_id = f"indicator--00000000-0000-4000-8000-{n:012d}"
    now = datetime.now(UTC)
    return StixEntity(
        id=0,
        bucket_id=bucket_id,
        stix_id=stix_id,
        type="indicator",
        spec_version="2.1",
        creator="collector",
        value=f"pattern-{n}",
        platform_modified=now,
        platform_created=now,
        object={"id": stix_id, "type": "indicator", "labels": []},
        other_stix_ids=[f"indicator--original-{n}"],
    )


async def _setup(
    worker: Worker, bucket_names: tuple[str, ...], n_entities: int = 2
) -> tuple[InMemoryBucketRepository, dict[str, Bucket]]:
    repo = InMemoryBucketRepository()
    buckets = {name: await repo.save(Bucket(name=name)) for name in bucket_names}
    raw_id = buckets["raw"].id
    assert raw_id is not None
    await repo.add_entities(raw_id, [_indicator(raw_id, i) for i in range(n_entities)])
    return repo, buckets


async def test_results_are_appended_to_every_sink() -> None:
    worker = Worker(_config([_pipeline(["raw"], ["refined", "shared"])]))
    [pipeline] = worker.pipeline_manager.pipelines()
    repo, buckets = await _setup(worker, ("raw", "refined", "shared"))
    sources_before = copy.deepcopy(await repo.get_entities(bucket_name="raw"))

    processed = await worker.process_batch(pipeline, buckets["raw"], repo)

    assert processed == 2
    refined = await repo.get_entities(bucket_name="refined")
    shared = await repo.get_entities(bucket_name="shared")
    for sink, sink_entities in (("refined", refined), ("shared", shared)):
        assert len(sink_entities) == 2
        for entity, source in zip(sink_entities, sources_before, strict=True):
            assert entity.bucket_id == buckets[sink].id
            assert entity.status == ProcessingStatus.pending
            assert entity.creator == source.creator == "collector"
            assert entity.pipeline == "refine"
            assert entity.object["labels"] == ["refined"]
            assert entity.stix_id == source.stix_id
            assert entity.other_stix_ids == source.other_stix_ids
            assert entity.id != source.id
    # Sinks never share an object dict.
    assert refined[0].object is not shared[0].object


async def test_source_entities_marked_processed_and_unchanged() -> None:
    worker = Worker(_config([_pipeline(["raw"], ["refined"])]))
    [pipeline] = worker.pipeline_manager.pipelines()
    repo, buckets = await _setup(worker, ("raw", "refined"))
    objects_before = [
        copy.deepcopy(e.object) for e in await repo.get_entities(bucket_name="raw")
    ]

    await worker.process_batch(pipeline, buckets["raw"], repo)

    sources = await repo.get_entities(bucket_name="raw")
    assert [e.status for e in sources] == [ProcessingStatus.processed] * 2
    assert [e.pipeline for e in sources] == [None, None]
    assert [e.object for e in sources] == objects_before


async def test_batch_size_limits_processing() -> None:
    worker = Worker(_config([_pipeline(["raw"], ["refined"])]))
    [pipeline] = worker.pipeline_manager.pipelines()
    repo, buckets = await _setup(worker, ("raw", "refined"), n_entities=3)

    assert await worker.process_batch(pipeline, buckets["raw"], repo, n=2) == 2
    assert await worker.process_batch(pipeline, buckets["raw"], repo, n=2) == 1
    assert await worker.process_batch(pipeline, buckets["raw"], repo, n=2) == 0
    assert len(await repo.get_entities(bucket_name="refined")) == 3


async def test_empty_source_writes_nothing() -> None:
    worker = Worker(_config([_pipeline(["raw"], ["refined"])]))
    [pipeline] = worker.pipeline_manager.pipelines()
    repo, buckets = await _setup(worker, ("raw", "refined"), n_entities=0)

    assert await worker.process_batch(pipeline, buckets["raw"], repo) == 0
    assert await repo.get_entities(bucket_name="refined") == []


async def test_missing_sink_raises_before_any_write() -> None:
    worker = Worker(_config([_pipeline(["raw"], ["refined", "shared"])]))
    [pipeline] = worker.pipeline_manager.pipelines()
    # "shared" is configured but was never provisioned.
    repo, buckets = await _setup(worker, ("raw", "refined"))

    with pytest.raises(SinkNotFoundError, match="sink bucket 'shared' does not exist"):
        await worker.process_batch(pipeline, buckets["raw"], repo)

    assert await repo.get_entities(bucket_name="refined") == []


# --- cycle detection ---


def test_pipeline_chain_loads() -> None:
    config = _config(
        [
            _pipeline(["raw"], ["refined"], name="a"),
            _pipeline(["refined"], ["shared"], name="b"),
        ]
    )
    assert len(PipelineManager(config).pipelines()) == 2


def test_pipeline_self_loop_raises() -> None:
    config = _config([_pipeline(["raw"], ["raw"])])
    with pytest.raises(PipelineValidationError, match="cycle: raw -> raw"):
        PipelineManager(config)


def test_pipeline_cycle_across_pipelines_raises() -> None:
    config = _config(
        [
            _pipeline(["raw"], ["refined"], name="a"),
            _pipeline(["refined"], ["shared"], name="b"),
            _pipeline(["shared"], ["raw"], name="c"),
        ]
    )
    with pytest.raises(
        PipelineValidationError, match="cycle: raw -> refined -> shared -> raw"
    ):
        PipelineManager(config)


# --- polling loop ---


def _factory(repo: InMemoryBucketRepository) -> Any:
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[InMemoryBucketRepository]:
        yield repo

    return factory


async def test_run_drains_sources_then_stops() -> None:
    config = _config([_pipeline(["raw"], ["refined"])])
    worker = Worker(config, poll_interval=0.01, batch_size=4)
    repo, _ = await _setup(worker, ("raw", "refined"), n_entities=10)
    worker = Worker(config, poll_interval=0.01, batch_size=4, repo_factory=_factory(repo))

    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if len(await repo.get_entities(bucket_name="refined")) == 10:
            break
        await asyncio.sleep(0.01)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)

    assert len(await repo.get_entities(bucket_name="refined")) == 10
    sources = await repo.get_entities(bucket_name="raw")
    assert {e.status for e in sources} == {ProcessingStatus.processed}


async def test_run_picks_up_entities_arriving_while_idle() -> None:
    config = _config([_pipeline(["raw"], ["refined"])])
    worker = Worker(config)
    repo, buckets = await _setup(worker, ("raw", "refined"), n_entities=0)
    worker = Worker(config, poll_interval=0.01, repo_factory=_factory(repo))

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.05)  # idle: nothing pending yet
    raw_id = buckets["raw"].id
    assert raw_id is not None
    await repo.add_entities(raw_id, [_indicator(raw_id, 1)])
    for _ in range(100):
        if await repo.get_entities(bucket_name="refined"):
            break
        await asyncio.sleep(0.01)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)

    assert len(await repo.get_entities(bucket_name="refined")) == 1


async def test_stop_wakes_an_idle_worker() -> None:
    config = _config([_pipeline(["raw"], ["refined"])])
    worker = Worker(config)
    repo, _ = await _setup(worker, ("raw", "refined"), n_entities=0)
    worker = Worker(config, poll_interval=60, repo_factory=_factory(repo))

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.01)
    worker.stop()
    # Returns promptly instead of waiting out the 60s poll interval.
    await asyncio.wait_for(task, timeout=1)


async def test_failing_pipeline_does_not_stop_others() -> None:
    config = _config(
        [
            _pipeline(["other"], ["missing"], name="broken"),
            _pipeline(["raw"], ["refined"], name="ok"),
        ],
        buckets=("raw", "other", "refined", "missing"),
    )
    worker = Worker(config)
    # "missing" is configured but never provisioned, so pipeline "broken" fails.
    repo, buckets = await _setup(worker, ("raw", "other", "refined"), n_entities=3)
    other_id = buckets["other"].id
    assert other_id is not None
    await repo.add_entities(other_id, [_indicator(other_id, 99)])
    worker = Worker(config, repo_factory=_factory(repo))

    assert await worker.run_once() == 3
    assert len(await repo.get_entities(bucket_name="refined")) == 3
