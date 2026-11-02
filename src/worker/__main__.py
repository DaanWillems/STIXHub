
import asyncio
import logging

from config import load_platform_config
from database.database import db
from database.repositories.bucket import BucketRepository, DatabaseBucketRepository
from models.domain import ProcessingStatus
from permissions import get_effective_permissions
from pipeline.pipeline import PipelineExecutor
from pipeline.pipeline_manager import PipelineManager
from pipeline.refs import RefResolver

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self):
        self.config = load_platform_config()
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

    async def run(self):
        #for each pipeline
        for p in self.pipeline_manager.pipelines():
            for source in p.pipeline_definition.sources:
                #take 10 entities and process
                async with db.get_session() as session:
                    repo = DatabaseBucketRepository(session=session)
                    bucket = await repo.get(bucket_name=source)
                    if bucket is None:
                        logger.warning("tried reading from bucket that does not exist")
                        continue
                    entities = await repo.acquire_entities(bucket.id, 10)
                    logger.info(f"got entities! {entities}")

                    resolver = await self._resolver_for(p, repo)
                    new_entities = await p.execute_pipeline(
                        objects=entities, resolver=resolver
                    )
                    # TODO: write new_entities to the pipeline's sinks.
                    logger.info(f"processed entities: {new_entities}")

                    for e in entities:
                        e.status = ProcessingStatus.processed
                    await repo.update_entities(bucket.id, entities)


if __name__ == "__main__":
    print("Im working!")
    w = Worker()
    asyncio.run(w.run())
