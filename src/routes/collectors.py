"""Internal REST API used by collectors to register, push and read STIX objects.

Every call carries the collector's secret as a bearer token, and a
registration only ever touches the one bucket its config grants it, with the
access it grants (see collector_auth.py and specs/misp-connector.md).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import NoResultFound

from collector_auth import BucketAccess, CollectorAuth, CollectorAuthError
from database.repositories.bucket import BucketRepository
from database.repositories.collector import CollectorRepository
from dependencies import (
    CredentialsDep,
    get_bucket_repo,
    get_collector_auth,
    get_collector_repo,
)
from models.domain import (
    Bucket,
    Collector,
    CollectorCursorUpdate,
    CollectorEntity,
    CollectorEntityLookup,
    CollectorEntityLookupResponse,
    CollectorEntityPage,
    CollectorRegisterRequest,
    CollectorResponse,
    StixEntity,
    TaxiiEnvelopeModel,
    TaxiiWriteStatusModel,
)
from services.ingest import ingest_objects

logger = logging.getLogger(__name__)

collectors_router = APIRouter(prefix="/collectors", tags=["Collectors"])

BucketRepoDep = Annotated[BucketRepository, Depends(get_bucket_repo)]
CollectorRepoDep = Annotated[CollectorRepository, Depends(get_collector_repo)]
CollectorAuthDep = Annotated[CollectorAuth, Depends(get_collector_auth)]


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


def _presented(credentials: CredentialsDep) -> str | None:
    return None if credentials is None else credentials.credentials


async def _authorized(
    collector_id: int,
    access: BucketAccess | None,
    auth: CollectorAuth,
    credentials: CredentialsDep,
    collector_repo: CollectorRepository,
    bucket_repo: BucketRepository,
) -> Collector:
    """The collector, if the caller holds its secret and its grant allows
    ``access`` (any access when ``None``) to the bucket it registered for."""
    collector = await _get_collector(collector_repo, collector_id)
    try:
        grant = auth.authenticate(collector.name, _presented(credentials))
    except CollectorAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    bucket = await bucket_repo.get(bucket_id=collector.bucket_id)
    if bucket is None or bucket.name != grant.bucket:
        raise HTTPException(
            status_code=403,
            detail=f"Collector '{collector.name}' may not use this bucket; register again",
        )
    if access is not None and grant.access is not access:
        raise HTTPException(
            status_code=403,
            detail=f"Collector '{collector.name}' may not {access.value} "
            f"bucket '{grant.bucket}'",
        )
    return collector


@collectors_router.post("/register")
async def register_collector(
    body: CollectorRegisterRequest,
    collector_repo: CollectorRepoDep,
    bucket_repo: BucketRepoDep,
    auth: CollectorAuthDep,
    credentials: CredentialsDep,
) -> CollectorResponse:
    try:
        grant = auth.authenticate(body.name, _presented(credentials))
    except CollectorAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if body.bucket_name != grant.bucket:
        raise HTTPException(
            status_code=403,
            detail=f"Collector '{body.name}' may not use bucket '{body.bucket_name}'",
        )
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
    auth: CollectorAuthDep,
    credentials: CredentialsDep,
) -> JSONResponse:
    collector = await _authorized(
        collector_id, BucketAccess.write, auth, credentials, collector_repo, bucket_repo
    )
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
    bucket_repo: BucketRepoDep,
    auth: CollectorAuthDep,
    credentials: CredentialsDep,
) -> CollectorResponse:
    await _authorized(
        collector_id, None, auth, credentials, collector_repo, bucket_repo
    )
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
        platform_created=entity.platform_created,
        other_stix_ids=entity.other_stix_ids,
        object=entity.object,
    )


@collectors_router.get("/{collector_id}/entities")
async def list_entities(
    collector_id: int,
    collector_repo: CollectorRepoDep,
    bucket_repo: BucketRepoDep,
    auth: CollectorAuthDep,
    credentials: CredentialsDep,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> CollectorEntityPage:
    """Entities of the collector's bucket with an ``id`` above ``after``.

    The page ends where a cursor could skip a row that is still being
    committed (see ``BucketRepository.get_entities_after``).
    """
    collector = await _authorized(
        collector_id, BucketAccess.read, auth, credentials, collector_repo, bucket_repo
    )
    entities = await bucket_repo.get_entities_after(collector.bucket_id, after, limit)
    return CollectorEntityPage(entities=[_entity_response(e) for e in entities])


@collectors_router.post("/{collector_id}/entities/lookup")
async def lookup_entities(
    collector_id: int,
    body: CollectorEntityLookup,
    collector_repo: CollectorRepoDep,
    bucket_repo: BucketRepoDep,
    auth: CollectorAuthDep,
    credentials: CredentialsDep,
) -> CollectorEntityLookupResponse:
    """The newest entity of the collector's bucket for each ref."""
    collector = await _authorized(
        collector_id, BucketAccess.read, auth, credentials, collector_repo, bucket_repo
    )
    found = await bucket_repo.find_latest_by_refs(collector.bucket_id, body.refs)
    return CollectorEntityLookupResponse(
        entities={ref: _entity_response(entity) for ref, entity in found.items()}
    )
