import asyncio
import contextlib
from collections.abc import AsyncGenerator

from fastapi import FastAPI

import uvicorn
from collectors.runner import create_runner
from collectors.validation import validate_collectors
from config import load_platform_config, settings
from database.database import db
from database.repositories.bucket import DatabaseBucketRepository
from routes.collectors import collectors_router
from routes.taxii2 import (
    provision_buckets,
    taxii2_router,
    validate_collections,
    validate_roles,
)
from routes.users import users_router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    platform_config = load_platform_config()
    app.state.platform_config = platform_config

    await db.create_tables()
    validate_roles(platform_config.buckets, platform_config.roles)
    validate_collectors(platform_config.buckets, platform_config.collectors)
    if settings.BUCKET_REPO_BACKEND == "database":
        async with db.get_session() as session:
            repo = DatabaseBucketRepository(session)
            await provision_buckets(repo, platform_config.buckets)
            app.state.active_collections = await validate_collections(
                repo, platform_config.collections
            )
    else:
        app.state.active_collections = {}

    # Collector threads register over HTTP once the server is accepting
    # connections, so they are started last and stopped first.
    runner = create_runner(platform_config.collectors, settings.PLATFORM_INTERNAL_URL)
    runner.start()
    try:
        yield
    finally:
        await asyncio.to_thread(runner.stop)
        await db.dispose()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(taxii2_router)
    app.include_router(users_router)
    app.include_router(collectors_router)
    return app


app = create_app()


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok"}