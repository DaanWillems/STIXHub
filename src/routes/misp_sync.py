"""Internal REST API holding the MISP connectors' sync records.

Unauthenticated like the collector API: reachable only from localhost / the
internal network (see specs/misp-connector.md).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from database.repositories.misp_sync import MispSyncRepository, OwnershipError
from dependencies import get_misp_sync_repo
from models.domain import (
    MispEventSync,
    MispEventSyncLookup,
    MispEventSyncLookupResponse,
    MispEventSyncResponse,
    MispEventSyncUpdate,
)

misp_sync_router = APIRouter(prefix="/misp-sync", tags=["MISP sync"])

MispSyncRepoDep = Annotated[MispSyncRepository, Depends(get_misp_sync_repo)]


def _to_response(record: MispEventSync) -> MispEventSyncResponse:
    return MispEventSyncResponse(
        connector=record.connector,
        event_uuid=record.event_uuid,
        origin=record.origin,
        misp_timestamp=record.misp_timestamp,
    )


@misp_sync_router.post("/{connector}/events/lookup")
async def lookup_events(
    connector: str, body: MispEventSyncLookup, repo: MispSyncRepoDep
) -> MispEventSyncLookupResponse:
    records = await repo.get_many(connector, body.event_uuids)
    return MispEventSyncLookupResponse(
        events=[_to_response(record) for record in records.values()]
    )


@misp_sync_router.put("/{connector}/events/{event_uuid}")
async def put_event(
    connector: str,
    event_uuid: str,
    body: MispEventSyncUpdate,
    repo: MispSyncRepoDep,
) -> MispEventSyncResponse:
    record = MispEventSync(
        connector=connector,
        event_uuid=event_uuid,
        origin=body.origin,
        misp_timestamp=body.misp_timestamp,
    )
    try:
        return _to_response(await repo.upsert(record))
    except OwnershipError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
