import hashlib
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fastapi import Request

from config import settings
from database import db
from models.domain import User
from models.domain import PlatformConfig
from repositories.bucket import (
    BucketRepository,
    DatabaseBucketRepository,
    InMemoryBucketRepository,
)
from repositories.user import DatabaseUserRepository, UserRepository

_in_memory_bucket_repo = InMemoryBucketRepository()
_bearer = HTTPBearer(auto_error=False)

_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


async def get_bucket_repo() -> AsyncGenerator[BucketRepository, None]:
    if settings.BUCKET_REPO_BACKEND == "memory":
        yield _in_memory_bucket_repo
    else:
        async with db.get_session() as session:
            yield DatabaseBucketRepository(session)


async def get_user_repo() -> AsyncGenerator[UserRepository, None]:
    async with db.get_session() as session:
        yield DatabaseUserRepository(session)


async def require_admin(credentials: _CredentialsDep) -> None:
    if credentials is None or credentials.credentials != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def get_current_user(
    credentials: _CredentialsDep,
    repo: Annotated[UserRepository, Depends(get_user_repo)],
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    key_hash = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    user = await repo.get_by_key_hash(key_hash)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


def get_platform_config(request: Request) -> PlatformConfig:
    result: PlatformConfig = request.app.state.platform_config
    return result
