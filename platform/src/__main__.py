import contextlib
from collections.abc import AsyncGenerator
from config import settings
from database import db
from platform_config import BUCKET_CONFIGS
from repositories.bucket import DatabaseBucketRepository
from routes.taxii2 import provision_buckets, taxii2_router, validate_collections

import uvicorn
from fastapi import FastAPI


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    await db.create_tables()
    if settings.BUCKET_REPO_BACKEND == "database":
        async with db.get_session() as session:
            repo = DatabaseBucketRepository(session)
            await provision_buckets(repo, BUCKET_CONFIGS)
            await validate_collections(repo)
    yield
    await db.dispose()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(taxii2_router)
    return app


app = create_app()


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("src.__main__:app")
