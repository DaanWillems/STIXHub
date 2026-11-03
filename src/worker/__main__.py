import asyncio
import contextlib
import copy
import logging
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime

from config import load_platform_config, settings
from database.database import db
from database.repositories.bucket import BucketRepository, DatabaseBucketRepository
from models.domain import Bucket, PlatformConfig, ProcessingStatus, StixEntity
from permissions import get_effective_permissions
from pipeline.pipeline import PipelineExecutor
from pipeline.pipeline_manager import PipelineManager
from pipeline.refs import RefResolver

logger = logging.getLogger(__name__)


class SinkNotFoundError(Exception):
    pass


RepoFactory = Callable[[], AbstractAsyncContextManager[BucketRepository]]


@contextlib.asynccontextmanager
async def _database_repo() -> AsyncIterator[BucketRepository]:
    """One transaction per batch: it commits on success and rolls back on error."""
    async with db.get_session() as session:
        yield DatabaseBucketRepository(session=session)


class Worker:
    def __init__(
        self,
        config: PlatformConfig | None = None,
        *,
        poll_interval: float | None = None,
        batch_size: int | None = None,
        repo_factory: RepoFactory = _database_repo,
    ) -> None:
        self.config = config if config is not None else load_platform_config()
        self.pipeline_manager = PipelineManager(platform_config=self.config)
        self.poll_interval = (
            poll_interval if poll_interval is not None else settings.WORKER_POLL_INTERVAL
        )
        self.batch_size = batch_size if batch_size is not None else settings.WORKER_BATCH_SIZE
        self._repo_factory = repo_factory
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Ask ``run`` to exit once the current batch is done."""
        self._stop.set()

    async def _resolver_for(
        self, pipeline: PipelineExecutor, repo: BucketRepository
    ) -> RefResolver:
        """Resolve refs only against buckets the pipeline's role can read."""
        can_read, _ = get_effective_permissions(
            [pipeline.pipeline_definition.role], self.config.roles
        )
        bucket_ids: list[int] = []
        for name in sorted(can_read):
            bucket = await repo.get(bucket_name=name)
            if bucket is not None and bucket.id is not None:
                bucket_ids.append(bucket.id)
        return RefResolver(repo, bucket_ids)

    async def _sinks_for(
        self, pipeline: PipelineExecutor, repo: BucketRepository
    ) -> list[Bucket]:
        sinks: list[Bucket] = []
        for name in pipeline.pipeline_definition.sinks:
            bucket = await repo.get(bucket_name=name)
            if bucket is None or bucket.id is None:
                raise SinkNotFoundError(
                    f"Pipeline '{pipeline.pipeline_definition.name}': "
                    f"sink bucket '{name}' does not exist"
                )
            sinks.append(bucket)
        return sinks

    async def process_batch(
        self,
        pipeline: PipelineExecutor,
        source: Bucket,
        repo: BucketRepository,
        n: int = 100,
    ) -> int:
        """Run ``pipeline`` on up to ``n`` pending entities of ``source``.

        Results are appended to every sink as new ``pending`` entities, so
        pipelines reading those sinks pick them up. The source entities are
        then marked ``processed``. Callers run this in a single transaction:
        if anything raises, nothing is written and the source entities stay
        ``pending``.
        """
        assert source.id is not None
        entities = await repo.acquire_entities(source.id, n)
        if not entities:
            return 0

        # Resolve every sink first, so a missing one fails before any write.
        sinks = await self._sinks_for(pipeline, repo)
        resolver = await self._resolver_for(pipeline, repo)
        results = await pipeline.execute_pipeline(objects=entities, resolver=resolver)

        now = datetime.now(UTC)
        for sink in sinks:
            assert sink.id is not None
            await repo.add_entities(
                sink.id,
                [
                    replace(
                        result,
                        id=0,
                        bucket_id=sink.id,
                        pipeline=pipeline.pipeline_definition.name,
                        status=ProcessingStatus.pending,
                        platform_created=now,
                        platform_modified=now,
                        object=copy.deepcopy(result.object),
                    )
                    for result in results
                ],
            )

        processed: list[StixEntity] = [
            replace(e, status=ProcessingStatus.processed) for e in entities
        ]
        await repo.update_entities(source.id, processed)
        logger.info(
            "Pipeline '%s' processed %d entities from '%s' into %s",
            pipeline.pipeline_definition.name,
            len(entities),
            source.name,
            [sink.name for sink in sinks],
        )
        return len(entities)

    async def run_once(self) -> int:
        """Process one batch for every (pipeline, source) pair.

        Each batch runs in its own transaction. A failing batch is logged and
        rolled back (its entities stay pending) without affecting the others.
        Returns the number of source entities processed.
        """
        total = 0
        for p in self.pipeline_manager.pipelines():
            name = p.pipeline_definition.name
            for source_name in p.pipeline_definition.sources:
                if self._stop.is_set():
                    return total
                try:
                    async with self._repo_factory() as repo:
                        source = await repo.get(bucket_name=source_name)
                        if source is None:
                            logger.warning(
                                "Pipeline '%s': source bucket '%s' does not exist",
                                name,
                                source_name,
                            )
                            continue
                        total += await self.process_batch(
                            p, source, repo, n=self.batch_size
                        )
                except SinkNotFoundError as exc:
                    logger.error("%s", exc)
                except Exception:
                    logger.exception(
                        "Pipeline '%s' failed on source '%s'", name, source_name
                    )
        return total

    async def run(self) -> None:
        """Process batches until ``stop`` is called.

        Passes run back to back while there is work; when a pass finds nothing
        pending, the worker sleeps ``poll_interval`` seconds (waking early on
        ``stop``).
        """
        logger.info(
            "Worker started: %d pipeline(s), batch size %d, poll interval %ss",
            len(self.pipeline_manager.pipelines()),
            self.batch_size,
            self.poll_interval,
        )
        while not self._stop.is_set():
            if await self.run_once() == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self.poll_interval)
        logger.info("Worker stopped")


async def main() -> None:
    """Run the worker until SIGINT or SIGTERM, finishing the current batch first."""
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
