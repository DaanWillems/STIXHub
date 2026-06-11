import base64
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from dependencies import get_bucket_repo
from models.domain import (
    TaxiiCollectionModel,
    TaxiiCollectionsRootResponseModel,
    TaxiiDiscoveryResponseModel,
    TaxiiErrorModel,
    TaxiiObjectResponseModel,
    TaxiiRootResponseModel,
)
from repositories.bucket import BucketRepository


taxii2_router = APIRouter(prefix="/taxii2", tags=["TAXII 2.1"])

type CollectionsRepository = dict[str, TaxiiCollectionModel]

BucketRepoDep = Annotated[BucketRepository, Depends(get_bucket_repo)]


def get_dummy_collections() -> CollectionsRepository:
    return {
        "70a16fcf-8146-2da8-be66-6ca6fb7280af": TaxiiCollectionModel(
            id="70a16fcf-8146-2da8-be66-6ca6fb7280af",
            title="Example collection",
            description="test",
            can_read=True,
            can_write=True,
            media_types=[],
        ),
    }


def _encode_cursor(offset: int) -> str:
    return base64.b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    return int(base64.b64decode(cursor.encode()).decode())


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
    dummy_collections: CollectionsRepository = Depends(get_dummy_collections),
) -> TaxiiCollectionsRootResponseModel:
    return TaxiiCollectionsRootResponseModel(
        collections=list(dummy_collections.values())
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
    dummy_collections: CollectionsRepository = Depends(get_dummy_collections),
) -> JSONResponse:
    if collection_id not in dummy_collections:
        return JSONResponse(
            status_code=404,
            content=TaxiiErrorModel(
                title="Collection Not Found",
                description=f"No collection with id '{collection_id}' exists.",
                http_status="404",
            ).model_dump(exclude_none=True),
        )

    collection = dummy_collections[collection_id]

    if not collection.can_read:
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
        bucket_name=collection.title, limit=limit + 1, offset=offset
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
