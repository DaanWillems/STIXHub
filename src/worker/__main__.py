import asyncio
import copy
import logging
from dataclasses import replace
from datetime import UTC, datetime

from config import load_platform_config
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


class Worker:
    def __init__(self, config: PlatformConfig | None = None) -> None:
        self.config = config if config is not None else load_platform_config()
        self.pipeline_manager = PipelineManager(platform_config=self.config)

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
        n: int = 10,
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

    async def run(self) -> None:
        for p in self.pipeline_manager.pipelines():
            for source_name in p.pipeline_definition.sources:
                try:
                    async with db.get_session() as session:
                        repo = DatabaseBucketRepository(session=session)
                        source = await repo.get(bucket_name=source_name)
                        if source is None:
                            logger.warning(
                                "Pipeline '%s': source bucket '%s' does not exist",
                                p.pipeline_definition.name,
                                source_name,
                            )
                            continue
                        await self.process_batch(p, source, repo)
                except SinkNotFoundError as exc:
                    # The transaction rolled back; the batch stays pending.
                    logger.error("%s", exc)


if __name__ == "__main__":
    asyncio.run(Worker().run())
