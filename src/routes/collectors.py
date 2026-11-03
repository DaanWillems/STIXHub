"""Internal REST API used by collectors to register and push STIX objects.

These endpoints are unauthenticated by design: they are meant to be reachable
only from localhost / the internal network (see specs/taxii-collector.md).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import NoResultFound

from dependencies import get_bucket_repo, get_collector_repo
from models.domain import (
    Bucket,
    Collector,
    CollectorEntity,
    CollectorEntityLookup,
    CollectorEntityLookupResponse,
    CollectorEntityPage,
    CollectorCursorUpdate,
    CollectorRegisterRequest,
    CollectorResponse,
    TaxiiEnvelopeModel,
    StixEntity,
    TaxiiWriteStatusModel,
)
from database.repositories.bucket import BucketRepository
from database.repositories.collector import CollectorRepository
from services.ingest import ingest_objects

logger = logging.getLogger(__name__)

collectors_router = APIRouter(prefix="/collectors", tags=["Collectors"])

BucketRepoDep = Annotated[BucketRepository, Depends(get_bucket_repo)]
CollectorRepoDep = Annotated[CollectorRepository, Depends(get_collector_repo)]


def _to_response(collector: Collector) -> CollectorResponse:
    return CollectorResponse(
        id=collector.id,
        name=collector.name,
        bucket_id=collector.bucket_id,
        cursor=collector.cursor,
    )


async def _find_bucket(repo: BucketRepository, bucket_name: str) -> Bucket | None:
    try:
        bucket: Bucket | None = await repo.get(bucket_name=bucket_name)
    except NoResultFound:
        return None
    return bucket


async def _get_collector(repo: CollectorRepository, collector_id: int) -> Collector:
    collector = await repo.get(collector_id)
    if collector is None:
        raise HTTPException(
            status_code=404, detail=f"Collector {collector_id} not found"
        )
    return collector


@collectors_router.post("/register")
async def register_collector(
    body: CollectorRegisterRequest,
    collector_repo: CollectorRepoDep,
    bucket_repo: BucketRepoDep,
) -> CollectorResponse:
    bucket = await _find_bucket(bucket_repo, body.bucket_name)
    if bucket is None or bucket.id is None:
        raise HTTPException(
            status_code=404, detail=f"Bucket '{body.bucket_name}' does not exist"
        )
    collector = await collector_repo.register(body.name, bucket.id)
    logger.info(
        "Collector '%s' registered (id=%s, bucket='%s', cursor=%s)",
        collector.name,
        collector.id,
        body.bucket_name,
        collector.cursor,
    )
    return _to_response(collector)


@collectors_router.post(
    "/{collector_id}/objects",
    status_code=202,
    response_model=TaxiiWriteStatusModel,
)
async def submit_objects(
    collector_id: int,
    bundle: TaxiiEnvelopeModel,
    collector_repo: CollectorRepoDep,
    bucket_repo: BucketRepoDep,
) -> JSONResponse:
    collector = await _get_collector(collector_repo, collector_id)
    bucket = await bucket_repo.get(bucket_id=collector.bucket_id)
    status = await ingest_objects(
        bucket_repo, bucket, bundle.objects, creator=f"collector:{collector.name}"
    )
    return JSONResponse(status_code=202, content=status.model_dump(exclude_none=True))


@collectors_router.put("/{collector_id}/cursor")
async def update_cursor(
    collector_id: int,
    body: CollectorCursorUpdate,
    collector_repo: CollectorRepoDep,
) -> CollectorResponse:
    collector = await collector_repo.update_cursor(collector_id, body.cursor)
    if collector is None:
        raise HTTPException(
            status_code=404, detail=f"Collector {collector_id} not found"
        )
    return _to_response(collector)


def _entity_response(entity: StixEntity) -> CollectorEntity:
    return CollectorEntity(
        id=entity.id,
        stix_id=entity.stix_id,
        type=entity.type,
        creator=entity.creator,
        other_stix_ids=entity.other_stix_ids,
        object=entity.object,
    )


@collectors_router.get("/{collector_id}/entities")
async def list_entities(
    collector_id: int,
    collector_repo: CollectorRepoDep,
    bucket_repo: BucketRepoDep,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> CollectorEntityPage:
    """Entities of the collector's bucket with an ``id`` above ``after``."""
    collector = await _get_collector(collector_repo, collector_id)
    entities = await bucket_repo.get_entities_after(collector.bucket_id, after, limit)
    return CollectorEntityPage(entities=[_entity_response(e) for e in entities])


@collectors_router.post("/{collector_id}/entities/lookup")
async def lookup_entities(
    collector_id: int,
    body: CollectorEntityLookup,
    collector_repo: CollectorRepoDep,
    bucket_repo: BucketRepoDep,
) -> CollectorEntityLookupResponse:
    """The newest entity of the collector's bucket for each ref."""
    collector = await _get_collector(collector_repo, collector_id)
    found = await bucket_repo.find_latest_by_refs(collector.bucket_id, body.refs)
    return CollectorEntityLookupResponse(
        entities={ref: _entity_response(entity) for ref, entity in found.items()}
    )
