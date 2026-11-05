"""Internal REST API used by collectors to register and push STIX objects.

Every request must carry ``Authorization: Bearer <secret>``, where the secret
is the shared secret of a collector declared in the platform config (env var
``STIX_HUB_COLLECTOR_SECRETS__<secret_env>``). A collector may only register
under its own name into its configured ``target_bucket``, and only act on its
own registration.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.exc import NoResultFound

from dependencies import get_bucket_repo, get_collector_repo, require_collector
from models.domain import (
    Bucket,
    Collector,
    CollectorConfig,
    CollectorCursorUpdate,
    CollectorRegisterRequest,
    CollectorResponse,
    TaxiiEnvelopeModel,
    TaxiiWriteStatusModel,
)
from database.repositories.bucket import BucketRepository
from database.repositories.collector import CollectorRepository
from services.ingest import ingest_objects

logger = logging.getLogger(__name__)

collectors_router = APIRouter(prefix="/collectors", tags=["Collectors"])

BucketRepoDep = Annotated[BucketRepository, Depends(get_bucket_repo)]
CollectorRepoDep = Annotated[CollectorRepository, Depends(get_collector_repo)]
CollectorConfigDep = Annotated[CollectorConfig, Depends(require_collector)]

_FORBIDDEN = "Not authorized for this collector"


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


async def _get_own_collector(
    repo: CollectorRepository, collector_id: int, config: CollectorConfig
) -> Collector:
    collector = await repo.get(collector_id)
    if collector is None:
        raise HTTPException(
            status_code=404, detail=f"Collector {collector_id} not found"
        )
    if collector.name != config.name:
        raise HTTPException(status_code=403, detail=_FORBIDDEN)
    return collector


@collectors_router.post("/register")
async def register_collector(
    body: CollectorRegisterRequest,
    collector_repo: CollectorRepoDep,
    bucket_repo: BucketRepoDep,
    config: CollectorConfigDep,
) -> CollectorResponse:
    if body.name != config.name:
        raise HTTPException(status_code=403, detail=_FORBIDDEN)
    bucket = await _find_bucket(bucket_repo, body.bucket_name)
    if bucket is None or bucket.id is None:
        raise HTTPException(
            status_code=404, detail=f"Bucket '{body.bucket_name}' does not exist"
        )
    if body.bucket_name != config.target_bucket:
        raise HTTPException(status_code=403, detail=_FORBIDDEN)
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
    config: CollectorConfigDep,
) -> JSONResponse:
    collector = await _get_own_collector(collector_repo, collector_id, config)
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
    config: CollectorConfigDep,
) -> CollectorResponse:
    await _get_own_collector(collector_repo, collector_id, config)
    collector = await collector_repo.update_cursor(collector_id, body.cursor)
    if collector is None:
        raise HTTPException(
            status_code=404, detail=f"Collector {collector_id} not found"
        )
    return _to_response(collector)
