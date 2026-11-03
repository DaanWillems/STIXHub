from typing import Any

import httpx

from models.domain import (
    CollectorEntity,
    CollectorEntityLookupResponse,
    CollectorEntityPage,
    CollectorResponse,
    MispEventOrigin,
    MispEventSync,
    MispEventSyncLookupResponse,
    TaxiiWriteStatusModel,
)


class PlatformClient:
    """Synchronous client for the platform's internal collector API.

    Every request carries the collector's ``secret`` as a bearer token, so
    pointing ``base_url`` at a remote host is all that is needed to run
    collectors as a separate service.
    """

    def __init__(self, http: httpx.Client, base_url: str, secret: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"}

    def register(self, name: str, bucket_name: str) -> CollectorResponse:
        response = self._http.post(
            f"{self._base_url}/collectors/register",
            headers=self._headers,
            json={"name": name, "bucket_name": bucket_name},
        )
        response.raise_for_status()
        return CollectorResponse.model_validate(response.json())

    def submit_objects(
        self, collector_id: int, objects: list[dict[str, Any]]
    ) -> TaxiiWriteStatusModel:
        response = self._http.post(
            f"{self._base_url}/collectors/{collector_id}/objects",
            headers=self._headers,
            json={"objects": objects},
        )
        response.raise_for_status()
        return TaxiiWriteStatusModel.model_validate(response.json())

    def update_cursor(self, collector_id: int, cursor: str) -> None:
        response = self._http.put(
            f"{self._base_url}/collectors/{collector_id}/cursor",
            headers=self._headers,
            json={"cursor": cursor},
        )
        response.raise_for_status()

    def list_entities(
        self, collector_id: int, after: int, limit: int
    ) -> list[CollectorEntity]:
        response = self._http.get(
            f"{self._base_url}/collectors/{collector_id}/entities",
            headers=self._headers,
            params={"after": after, "limit": limit},
        )
        response.raise_for_status()
        return CollectorEntityPage.model_validate(response.json()).entities

    def lookup_entities(
        self, collector_id: int, refs: list[str]
    ) -> dict[str, CollectorEntity]:
        response = self._http.post(
            f"{self._base_url}/collectors/{collector_id}/entities/lookup",
            headers=self._headers,
            json={"refs": refs},
        )
        response.raise_for_status()
        return CollectorEntityLookupResponse.model_validate(response.json()).entities

    def lookup_misp_events(
        self, connector: str, event_uuids: list[str]
    ) -> dict[str, MispEventSync]:
        response = self._http.post(
            f"{self._base_url}/misp-sync/{connector}/events/lookup",
            headers=self._headers,
            json={"event_uuids": event_uuids},
        )
        response.raise_for_status()
        events = MispEventSyncLookupResponse.model_validate(response.json()).events
        return {
            event.event_uuid: MispEventSync(
                connector=event.connector,
                event_uuid=event.event_uuid,
                origin=event.origin,
                misp_timestamp=event.misp_timestamp,
            )
            for event in events
        }

    def put_misp_event(
        self,
        connector: str,
        event_uuid: str,
        origin: MispEventOrigin,
        misp_timestamp: int | None,
    ) -> None:
        response = self._http.put(
            f"{self._base_url}/misp-sync/{connector}/events/{event_uuid}",
            headers=self._headers,
            json={"origin": origin.value, "misp_timestamp": misp_timestamp},
        )
        response.raise_for_status()
