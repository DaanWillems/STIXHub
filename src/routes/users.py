import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_platform_config, get_user_repo, require_admin
from models.domain import (
    PlatformConfig,
    User,
    UserCreate,
    UserCreateResponse,
    UserPatch,
    UserResponse,
)
from database.repositories.user import UserRepository

users_router = APIRouter(prefix="/users", tags=["Users"])

UserRepoDep = Annotated[UserRepository, Depends(get_user_repo)]
AdminDep = Annotated[None, Depends(require_admin)]


@users_router.post("/", status_code=201)
async def create_user(
    _: AdminDep,
    body: UserCreate,
    repo: UserRepoDep,
    platform_config: PlatformConfig = Depends(get_platform_config),
) -> UserCreateResponse:
    for role in body.roles:
        if role not in [role.name for role in platform_config.roles]:
            raise HTTPException(status_code=422, detail=f"Unknown role: '{role}'")

    if await repo.get_by_email(body.email) is not None:
        raise HTTPException(
            status_code=409, detail=f"Email '{body.email}' already exists"
        )

    api_key = secrets.token_hex(32)
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    user = User(
        id=uuid.uuid4(),
        email=body.email,
        roles=body.roles,
        created_at=datetime.now(timezone.utc),
    )
    saved = await repo.save(user, api_key_hash)

    return UserCreateResponse(
        id=saved.id,
        email=saved.email,
        roles=saved.roles,
        created_at=saved.created_at,
        api_key=api_key,
    )


@users_router.get("/")
async def list_users(
    _: AdminDep,
    repo: UserRepoDep,
) -> list[UserResponse]:
    users = await repo.list_all()
    return [
        UserResponse(id=u.id, email=u.email, roles=u.roles, created_at=u.created_at)
        for u in users
    ]


@users_router.patch("/{user_id}")
async def patch_user(
    user_id: UUID,
    _: AdminDep,
    body: UserPatch,
    repo: UserRepoDep,
    platform_config: PlatformConfig = Depends(get_platform_config),
) -> UserResponse:
    for role in body.roles:
        if role not in [role.name for role in platform_config.roles]:
            raise HTTPException(status_code=422, detail=f"Unknown role: '{role}'")

    if await repo.get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

    updated = await repo.update_roles(user_id, body.roles)
    return UserResponse(
        id=updated.id,
        email=updated.email,
        roles=updated.roles,
        created_at=updated.created_at,
    )


@users_router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    _: AdminDep,
    repo: UserRepoDep,
) -> None:
    if await repo.get_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

    await repo.delete(user_id)
