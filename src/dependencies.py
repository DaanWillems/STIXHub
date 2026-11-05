import hashlib
import hmac
from collections.abc import AsyncGenerator, Mapping
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fastapi import Request
from pydantic import SecretStr

from collectors.validation import find_secret
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
from database.repositories.user import DatabaseUserRepository, UserRepository
from database.database import db

_in_memory_bucket_repo = InMemoryBucketRepository()
_in_memory_collector_repo = InMemoryCollectorRepository()
_bearer = HTTPBearer(auto_error=False)

_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


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


def get_collector_secrets() -> Mapping[str, SecretStr]:
    return settings.COLLECTOR_SECRETS


async def require_collector(
    credentials: _CredentialsDep,
    platform_config: Annotated[PlatformConfig, Depends(get_platform_config)],
    secrets: Annotated[Mapping[str, SecretStr], Depends(get_collector_secrets)],
) -> str:
    """Authenticate a collector by its shared secret; returns the collector name.

    The bearer token must equal the secret of a collector declared in the
    platform config (``STIX_HUB_COLLECTOR_SECRETS__<secret_env>``).
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    presented = credentials.credentials.encode()
    authenticated: str | None = None
    # Compare against every secret so timing does not reveal which matched.
    for config in platform_config.collectors:
        secret = find_secret(config, secrets)
        if secret is not None and hmac.compare_digest(presented, secret.encode()):
            authenticated = config.name
    if authenticated is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return authenticated
