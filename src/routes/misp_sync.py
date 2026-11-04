"""Internal REST API holding the MISP connectors' sync records.

Like the collector API, every call carries the connector's secret as a bearer
token (see collector_auth.py and specs/misp-connector.md).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from collector_auth import CollectorAuth, CollectorAuthError
from database.repositories.misp_sync import MispSyncRepository, OwnershipError
from dependencies import CredentialsDep, get_collector_auth, get_misp_sync_repo
from models.domain import (
    MispEventRecord,
    MispEventSync,
    MispEventSyncLookup,
    MispEventSyncLookupResponse,
    MispEventSyncUpdate,
)

misp_sync_router = APIRouter(prefix="/misp-sync", tags=["MISP sync"])

MispSyncRepoDep = Annotated[MispSyncRepository, Depends(get_misp_sync_repo)]
CollectorAuthDep = Annotated[CollectorAuth, Depends(get_collector_auth)]


def _authenticate(
    connector: str, auth: CollectorAuth, credentials: CredentialsDep
) -> str:
    """The connector's MISP instance, if the caller holds the connector's secret."""
    presented = None if credentials is None else credentials.credentials
    try:
        return auth.misp_instance(connector, presented)
    except CollectorAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _to_response(record: MispEventSync) -> MispEventRecord:
    return MispEventRecord(
        event_uuid=record.event_uuid,
        origin=record.origin,
        misp_timestamp=record.misp_timestamp,
        creator_org=record.creator_org,
    )


@misp_sync_router.post("/{connector}/events/lookup")
async def lookup_events(
    connector: str,
    body: MispEventSyncLookup,
    repo: MispSyncRepoDep,
    auth: CollectorAuthDep,
    credentials: CredentialsDep,
) -> MispEventSyncLookupResponse:
    instance = _authenticate(connector, auth, credentials)
    records = await repo.get_many(instance, body.event_uuids)
    return MispEventSyncLookupResponse(
        events=[_to_response(record) for record in records.values()]
    )


@misp_sync_router.put("/{connector}/events/{event_uuid}")
async def put_event(
    connector: str,
    event_uuid: str,
    body: MispEventSyncUpdate,
    repo: MispSyncRepoDep,
    auth: CollectorAuthDep,
    credentials: CredentialsDep,
) -> MispEventRecord:
    record = MispEventSync(
        instance=_authenticate(connector, auth, credentials),
        event_uuid=event_uuid,
        origin=body.origin,
        misp_timestamp=body.misp_timestamp,
        creator_org=body.creator_org,
    )
    try:
        return _to_response(await repo.upsert(record))
    except OwnershipError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
