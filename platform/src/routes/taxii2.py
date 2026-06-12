import base64
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from dependencies import get_bucket_repo
from models.domain import (
    BucketMode,
    CollectionConfig,
    StixEntity,
    TaxiiCollectionModel,
    TaxiiCollectionsRootResponseModel,
    TaxiiDiscoveryResponseModel,
    TaxiiErrorModel,
    TaxiiObjectResponseModel,
    TaxiiRootResponseModel,
    TaxiiStatusRef,
    TaxiiWriteStatusModel,
)
from processors import stix as stix_processor
from repositories.bucket import BucketRepository

logger = logging.getLogger(__name__)

taxii2_router = APIRouter(prefix="/taxii2", tags=["TAXII 2.1"])

type CollectionsRepository = dict[str, CollectionConfig]

BucketRepoDep = Annotated[BucketRepository, Depends(get_bucket_repo)]

_COLLECTION_CONFIGS: CollectionsRepository = {
    "70a16fcf-8146-2da8-be66-6ca6fb7280af": CollectionConfig(
        taxii_collection=TaxiiCollectionModel(
            id="70a16fcf-8146-2da8-be66-6ca6fb7280af",
            title="Example collection",
            description="test",
            can_read=True,
            can_write=True,
            media_types=[],
        ),
        bucket_name="Example collection",
        mode=BucketMode.append,
    ),
}

_active_configs: CollectionsRepository = dict(_COLLECTION_CONFIGS)


async def validate_collections(repo: BucketRepository) -> None:
    global _active_configs
    valid: CollectionsRepository = {}
    for cid, config in _COLLECTION_CONFIGS.items():
        try:
            await repo.get(bucket_name=config.bucket_name)
            valid[cid] = config
        except Exception:
            logger.error(
                "Collection '%s' ('%s') disabled: bucket '%s' not found",
                cid,
                config.taxii_collection.title,
                config.bucket_name,
            )
    _active_configs = valid


def get_dummy_collections() -> CollectionsRepository:
    return _active_configs


def _encode_cursor(offset: int) -> str:
    return base64.b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    return int(base64.b64decode(cursor.encode()).decode())


def _validate_stix_object(obj: dict[str, Any]) -> None:
    for field_name in ("id", "type", "spec_version"):
        if not obj.get(field_name):
            raise ValueError(f"Missing required field: '{field_name}'")


@taxii2_router.get("/")
def taxii_discovery(request: Request) -> TaxiiDiscoveryResponseModel:
    return TaxiiDiscoveryResponseModel(
        title="StixHub TAXII2.1 Server",
        description="This server is used for exchanging CTI with StixHub",
        contact="n.a",
        default=f"{request.url}root/",
        api_roots=[f"{request.url}root/"],
    )


@taxii2_router.get("/root/")
def taxii_root() -> TaxiiRootResponseModel:
    return TaxiiRootResponseModel(
        title="Root 1", description="Description root 1", max_content_length=1
    )


@taxii2_router.get("/root/collections/")
def taxii_collections_root(
    configs: CollectionsRepository = Depends(get_dummy_collections),
) -> TaxiiCollectionsRootResponseModel:
    return TaxiiCollectionsRootResponseModel(
        collections=[c.taxii_collection for c in configs.values()]
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
    repo: BucketRepoDep,
    limit: int = Query(default=20, ge=1, le=1000),
    next_cursor: str | None = Query(default=None, alias="next"),
    configs: CollectionsRepository = Depends(get_dummy_collections),
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

    if not config.taxii_collection.can_read:
        return JSONResponse(
            status_code=403,
            content=TaxiiErrorModel(
                title="Forbidden",
                description=f"Collection '{collection_id}' does not permit reading.",
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
    request: Request,
    repo: BucketRepoDep,
    configs: CollectionsRepository = Depends(get_dummy_collections),
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

    if not config.taxii_collection.can_write:
        return JSONResponse(
            status_code=403,
            content=TaxiiErrorModel(
                title="Forbidden",
                description=f"Collection '{collection_id}' does not permit writing.",
                http_status="403",
            ).model_dump(exclude_none=True),
        )

    body = await request.json()
    raw_objects: list[dict[str, Any]] = body.get("objects", [])

    now = datetime.now(timezone.utc)
    bucket = await repo.get(bucket_name=config.bucket_name)

    successes: list[TaxiiStatusRef] = []
    failures: list[TaxiiStatusRef] = []
    entities_to_add: list[StixEntity] = []

    for raw_obj in raw_objects:
        obj_id: str = raw_obj.get("id", "unknown")
        try:
            _validate_stix_object(raw_obj)
            processed = stix_processor.process(raw_obj)
            entities_to_add.append(
                StixEntity(
                    id=0,
                    bucket_id=bucket.id,
                    stix_id=processed.platform_id,
                    type=raw_obj["type"],
                    spec_version=raw_obj["spec_version"],
                    creator=collection_id,
                    value=processed.value,
                    platform_modified=now,
                    platform_created=now,
                    other_stix_ids=processed.other_stix_ids,
                    object=processed.object,
                )
            )
            successes.append(
                TaxiiStatusRef(
                    id=processed.platform_id,
                    version=raw_obj.get("modified"),
                )
            )
        except (NotImplementedError, ValueError, KeyError) as exc:
            failures.append(TaxiiStatusRef(id=obj_id, message=str(exc)))

    if entities_to_add:
        await repo.add_entities(bucket.id, entities_to_add)

    status_str: Literal["complete", "complete_with_errors"] = (
        "complete_with_errors" if failures else "complete"
    )

    return JSONResponse(
        status_code=202,
        content=TaxiiWriteStatusModel(
            id=str(uuid.uuid4()),
            status=status_str,
            request_timestamp=now.isoformat(),
            total_count=len(raw_objects),
            success_count=len(successes),
            failure_count=len(failures),
            pending_count=0,
            successes=successes,
            failures=failures,
        ).model_dump(exclude_none=True),
    )
