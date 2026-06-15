import contextlib
from collections.abc import AsyncGenerator

from fastapi import FastAPI

import uvicorn
from server.config import load_platform_config, settings
from server.database import db
from server.repositories.bucket import DatabaseBucketRepository
from server.routes.taxii2 import (
    provision_buckets,
    taxii2_router,
    validate_collections,
    validate_roles,
)
from server.routes.users import users_router


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    platform_config = load_platform_config()
    app.state.platform_config = platform_config

    await db.create_tables()
    validate_roles(platform_config.buckets, platform_config.roles)
    if settings.BUCKET_REPO_BACKEND == "database":
        async with db.get_session() as session:
            repo = DatabaseBucketRepository(session)
            await provision_buckets(repo, platform_config.buckets)
            app.state.active_collections = await validate_collections(
                repo, platform_config.collections
            )
    else:
        app.state.active_collections = {}
    yield
    await db.dispose()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(taxii2_router)
    app.include_router(users_router)
    return app


app = create_app()


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("server.__main__:app")
