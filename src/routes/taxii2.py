import base64
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from dependencies import get_bucket_repo, get_current_user, get_platform_config
from models.domain import (
    Bucket,
    BucketConfig,
    BucketMode,
    CollectionConfig,
    PlatformConfig,
    RoleConfig,
    TaxiiCollectionModel,
    TaxiiCollectionsRootResponseModel,
    TaxiiDiscoveryResponseModel,
    TaxiiEnvelopeModel,
    TaxiiErrorModel,
    TaxiiObjectResponseModel,
    TaxiiRootResponseModel,
    TaxiiWriteStatusModel,
    User,
)
from database.repositories.bucket import BucketRepository
from services.ingest import ingest_objects

logger = logging.getLogger(__name__)

taxii2_router = APIRouter(prefix="/taxii2", tags=["TAXII 2.1"])

type CollectionsRepository = dict[str, CollectionConfig]

BucketRepoDep = Annotated[BucketRepository, Depends(get_bucket_repo)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]


def _get_effective_permissions(
    user_roles: list[str], role_configs: list[RoleConfig]
) -> tuple[set[str], set[str]]:
    can_read: set[str] = set()
    can_write: set[str] = set()

    role_map = {role.name: role for role in role_configs}

    for role_name in user_roles:
        if role := role_map.get(role_name):
            can_read.update(role.can_read)
            can_write.update(role.can_write)
    return can_read, can_write


async def provision_buckets(
    repo: BucketRepository, configs: list[BucketConfig]
) -> None:
    for config in configs:
        existing: Bucket | None = None
        try:
            existing = await repo.get(bucket_name=config.name)
        except Exception:
            pass
        if existing is None:
            await repo.save(Bucket(name=config.name, mode=config.mode))
            logger.info(
                "Provisioned bucket '%s' (mode=%s)", config.name, config.mode.value
            )
        else:
            if existing.mode == BucketMode.append and config.mode == BucketMode.merge:
                raise RuntimeError(
                    f"Invalid mode transition for bucket '{config.name}': "
                    f"cannot change from append to merge"
                )
            if existing.mode != config.mode:
                await repo.update_mode(bucket_id=existing.id, mode=config.mode)
                logger.info(
                    "Updated bucket '%s' mode: %s -> %s",
                    config.name,
                    existing.mode.value,
                    config.mode.value,
                )


async def validate_collections(
    repo: BucketRepository, configs: list[CollectionConfig]
) -> CollectionsRepository:
    valid: CollectionsRepository = {}
    for config in configs:
        try:
            await repo.get(bucket_name=config.bucket_name)
            valid[config.id] = config
        except Exception:
            logger.error(
                "Collection '%s' ('%s') disabled: bucket '%s' not found",
                config.id,
                config.title,
                config.bucket_name,
            )
    return valid


def validate_roles(
    bucket_configs: list[BucketConfig],
    role_configs: list[RoleConfig],
) -> None:
    known_buckets = set([bucket.name for bucket in bucket_configs])
    for role in role_configs:
        for bucket_name in role.can_read + role.can_write:
            if bucket_name not in known_buckets:
                raise RuntimeError(
                    f"Role '{role.name}' references unknown bucket '{bucket_name}'"
                )


def get_active_collections(request: Request) -> CollectionsRepository:
    result: CollectionsRepository = request.app.state.active_collections
    return result


def _encode_cursor(offset: int) -> str:
    return base64.b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    return int(base64.b64decode(cursor.encode()).decode())


@taxii2_router.get("/")
def taxii_discovery(request: Request, _: CurrentUserDep) -> TaxiiDiscoveryResponseModel:
    return TaxiiDiscoveryResponseModel(
        title="StixHub TAXII2.1 Server",
        description="This server is used for exchanging CTI with StixHub",
        contact="n.a",
        default=f"{request.url}root/",
        api_roots=[f"{request.url}root/"],
    )


@taxii2_router.get("/root/")
def taxii_root(_: CurrentUserDep) -> TaxiiRootResponseModel:
    return TaxiiRootResponseModel(
        title="Root 1", description="Description root 1", max_content_length=1
    )


@taxii2_router.get("/root/collections/")
def taxii_collections_root(
    user: CurrentUserDep,
    configs: CollectionsRepository = Depends(get_active_collections),
    platform_config: PlatformConfig = Depends(get_platform_config),
) -> TaxiiCollectionsRootResponseModel:
    can_read, can_write = _get_effective_permissions(user.roles, platform_config.roles)
    accessible = can_read | can_write
    return TaxiiCollectionsRootResponseModel(
        collections=[
            TaxiiCollectionModel(
                id=c.id,
                title=c.title,
                description=c.description,
                can_read=c.can_read,
                can_write=c.can_write,
            )
            for c in configs.values()
            if c.bucket_name in accessible
        ]
    )


@taxii2_router.get(
    "/root/collections/{collection_id}/objects/",
    response_model=None,
    responses={
        200: {"model": TaxiiObjectResponseModel},
        400: {"model": TaxiiErrorModel},
        403: {"model": TaxiiErrorModel},
        404: {"model": TaxiiErrorModel},
    },
)
async def get_collection_objects(
    collection_id: str,
    user: CurrentUserDep,
    repo: BucketRepoDep,
    limit: int = Query(default=20, ge=1, le=1000),
    next_cursor: str | None = Query(default=None, alias="next"),
    configs: CollectionsRepository = Depends(get_active_collections),
    platform_config: PlatformConfig = Depends(get_platform_config),
) -> JSONResponse:
    if collection_id not in configs:
        return JSONResponse(
            status_code=404,
            content=TaxiiErrorModel(
                title="Collection Not Found",
                description=f"No collection with id '{collection_id}' exists.",
                http_status="404",
            ).model_dump(exclude_none=True),
        )

    config = configs[collection_id]

    if not config.can_read:
        return JSONResponse(
            status_code=403,
            content=TaxiiErrorModel(
                title="Forbidden",
                description=f"Collection '{collection_id}' does not permit reading.",
                http_status="403",
            ).model_dump(exclude_none=True),
        )

    can_read, _ = _get_effective_permissions(user.roles, platform_config.roles)
    if config.bucket_name not in can_read:
        return JSONResponse(
            status_code=403,
            content=TaxiiErrorModel(
                title="Forbidden",
                description="You do not have read access to this collection.",
                http_status="403",
            ).model_dump(exclude_none=True),
        )

    offset = 0
    if next_cursor is not None:
        try:
            offset = _decode_cursor(next_cursor)
        except Exception:
            return JSONResponse(
                status_code=400,
                content=TaxiiErrorModel(
                    title="Invalid Cursor",
                    description="The 'next' pagination cursor could not be decoded.",
                    http_status="400",
                ).model_dump(exclude_none=True),
            )

    entities = await repo.get_entities(
        bucket_name=config.bucket_name, limit=limit + 1, offset=offset
    )

    more = len(entities) > limit
    page = entities[:limit] if more else entities
    next_out = _encode_cursor(offset + limit) if more else None

    return JSONResponse(
        content=TaxiiObjectResponseModel(
            more=more,
            next=next_out,
            objects=[e.object for e in page],
        ).model_dump(exclude_none=True),
    )


@taxii2_router.post(
    "/root/collections/{collection_id}/objects/",
    response_model=None,
    responses={
        202: {"model": TaxiiWriteStatusModel},
        403: {"model": TaxiiErrorModel},
        404: {"model": TaxiiErrorModel},
    },
)
async def add_collection_objects(
    collection_id: str,
    bundle: TaxiiEnvelopeModel,
    user: CurrentUserDep,
    repo: BucketRepoDep,
    configs: CollectionsRepository = Depends(get_active_collections),
    platform_config: PlatformConfig = Depends(get_platform_config),
) -> JSONResponse:
    if collection_id not in configs:
        return JSONResponse(
            status_code=404,
            content=TaxiiErrorModel(
                title="Collection Not Found",
                description=f"No collection with id '{collection_id}' exists.",
                http_status="404",
            ).model_dump(exclude_none=True),
        )

    config = configs[collection_id]

    if not config.can_write:
        return JSONResponse(
            status_code=403,
            content=TaxiiErrorModel(
                title="Forbidden",
                description=f"Collection '{collection_id}' does not permit writing.",
                http_status="403",
            ).model_dump(exclude_none=True),
        )

    _, can_write = _get_effective_permissions(user.roles, platform_config.roles)
    if config.bucket_name not in can_write:
        return JSONResponse(
            status_code=403,
            content=TaxiiErrorModel(
                title="Forbidden",
                description="You do not have write access to this collection.",
                http_status="403",
            ).model_dump(exclude_none=True),
        )

    bucket = await repo.get(bucket_name=config.bucket_name)
    status = await ingest_objects(repo, bucket, bundle.objects, creator=collection_id)

    return JSONResponse(
        status_code=202,
        content=status.model_dump(exclude_none=True),
    )
