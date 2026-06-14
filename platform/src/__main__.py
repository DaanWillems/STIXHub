import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path

import yaml
from fastapi import FastAPI
from pydantic import ValidationError

import uvicorn
from config import settings
from database import db
from platform_config import BUCKET_CONFIGS, ROLE_CONFIGS
from models.domain import PlatformConfig
from repositories.bucket import DatabaseBucketRepository
from routes.taxii2 import (
    provision_buckets,
    taxii2_router,
    validate_collections,
    validate_roles,
)
from routes.users import users_router


def _load_platform_config() -> PlatformConfig:
    config_path = Path(settings.PLATFORM_CONFIG)
    try:
        raw = yaml.safe_load(config_path.read_text())
    except FileNotFoundError:
        raise RuntimeError(f"Platform config file not found: {config_path.resolve()}")
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"Invalid YAML in platform config at {config_path.resolve()}: {exc}"
        )
    try:
        return PlatformConfig.model_validate(raw)
    except ValidationError as exc:
        raise RuntimeError(
            f"Invalid platform config at {config_path.resolve()}:\n{exc}"
        )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    platform_config = _load_platform_config()
    app.state.platform_config = platform_config

    await db.create_tables()
    validate_roles(BUCKET_CONFIGS, ROLE_CONFIGS)
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
    uvicorn.run("src.__main__:app")
