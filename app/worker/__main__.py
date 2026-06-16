
import asyncio

from server.models.domain import ProcessingStatus
from server.repositories.bucket import DatabaseBucketRepository
from database.database import Database, db

class Worker:
    def __init__(self):
        pass

    async def run(self):
        #for each pipeline
        #take 10 entities and process
        async with db.get_session() as session:
            repo = DatabaseBucketRepository(session=session)
            bucket = await repo.get(bucket_name="raw-intel")
            entities = await repo.acquire_entities(bucket.id, 100)
            print(f"got entities! {entities}")
            for e in entities:
                e.type = "test"
                e.status = ProcessingStatus.processed
            repo.add_entities(entities_in=entities)
        pass


if __name__ == "__main__":
    print("Im working!")
    w = Worker()
    asyncio.run(w.run())


