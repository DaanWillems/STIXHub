import hashlib
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fastapi import Request

from collector_auth import CollectorAuth
from config import settings
from models.domain import User
from models.domain import PlatformConfig
from database.repositories.bucket import (
    BucketRepository,
    DatabaseBucketRepository,
    InMemoryBucketRepository,
)
from database.repositories.collector import (
    CollectorRepository,
    DatabaseCollectorRepository,
    InMemoryCollectorRepository,
)
from database.repositories.misp_sync import (
    DatabaseMispSyncRepository,
    InMemoryMispSyncRepository,
    MispSyncRepository,
)
from database.repositories.user import DatabaseUserRepository, UserRepository
from database.database import db

_in_memory_bucket_repo = InMemoryBucketRepository()
_in_memory_collector_repo = InMemoryCollectorRepository()
_in_memory_misp_sync_repo = InMemoryMispSyncRepository()
_bearer = HTTPBearer(auto_error=False)

CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


async def get_bucket_repo() -> AsyncGenerator[BucketRepository, None]:
    if settings.BUCKET_REPO_BACKEND == "memory":
        yield _in_memory_bucket_repo
    else:
        async with db.get_session() as session:
            yield DatabaseBucketRepository(session)


async def get_collector_repo() -> AsyncGenerator[CollectorRepository, None]:
    if settings.BUCKET_REPO_BACKEND == "memory":
        yield _in_memory_collector_repo
    else:
        async with db.get_session() as session:
            yield DatabaseCollectorRepository(session)


async def get_misp_sync_repo() -> AsyncGenerator[MispSyncRepository]:
    if settings.BUCKET_REPO_BACKEND == "memory":
        yield _in_memory_misp_sync_repo
    else:
        async with db.get_session() as session:
            yield DatabaseMispSyncRepository(session)


async def get_user_repo() -> AsyncGenerator[UserRepository, None]:
    async with db.get_session() as session:
        yield DatabaseUserRepository(session)


async def require_admin(credentials: CredentialsDep) -> None:
    if credentials is None or credentials.credentials != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def get_current_user(
    credentials: CredentialsDep,
    repo: Annotated[UserRepository, Depends(get_user_repo)],
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    key_hash = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    user = await repo.get_by_key_hash(key_hash)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


def get_collector_auth(request: Request) -> CollectorAuth:
    auth: CollectorAuth = request.app.state.collector_auth
    return auth


def get_platform_config(request: Request) -> PlatformConfig:
    result: PlatformConfig = request.app.state.platform_config
    return result
