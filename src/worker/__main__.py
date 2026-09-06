
import asyncio

from config import load_platform_config
from models.domain import ProcessingStatus
from database.repositories.bucket import DatabaseBucketRepository
from database.database import Database, db

class Worker:
    def __init__(self):
        self.config = load_platform_config()

    async def run(self):
        #for each pipeline
        for p in self.config.pipelines:
            for source in p.sources:
                #take 10 entities and process
                async with db.get_session() as session:
                    repo = DatabaseBucketRepository(session=session)
                    bucket = await repo.get(bucket_name=source)
                    if bucket is None:
                        print("tried reading from bucket that does not exist")
                        continue
                    entities = await repo.acquire_entities(bucket.id, 100)
                    print(f"got entities! {entities}")
                    for e in entities:
                        e.type = "test"
                        e.status = ProcessingStatus.processed
                    await repo.update_entities(bucket.id, entities)
                pass


if __name__ == "__main__":
    print("Im working!")
    w = Worker()
    asyncio.run(w.run())


