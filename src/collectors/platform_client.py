from typing import Any

import httpx

from models.domain import CollectorResponse, TaxiiWriteStatusModel


class PlatformClient:
    """Synchronous client for the platform's internal collector API.

    Requests authenticate with the collector's shared ``secret`` as a bearer
    token, so pointing ``base_url`` at a remote host is all that is needed to
    run collectors as a separate service.
    """

    def __init__(self, http: httpx.Client, base_url: str, secret: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"}

    def register(self, name: str, bucket_name: str) -> CollectorResponse:
        response = self._http.post(
            f"{self._base_url}/collectors/register",
            json={"name": name, "bucket_name": bucket_name},
            headers=self._headers,
        )
        response.raise_for_status()
        return CollectorResponse.model_validate(response.json())

    def submit_objects(
        self, collector_id: int, objects: list[dict[str, Any]]
    ) -> TaxiiWriteStatusModel:
        response = self._http.post(
            f"{self._base_url}/collectors/{collector_id}/objects",
            json={"objects": objects},
            headers=self._headers,
        )
        response.raise_for_status()
        return TaxiiWriteStatusModel.model_validate(response.json())

    def update_cursor(self, collector_id: int, cursor: str) -> None:
        response = self._http.put(
            f"{self._base_url}/collectors/{collector_id}/cursor",
            json={"cursor": cursor},
            headers=self._headers,
        )
        response.raise_for_status()
