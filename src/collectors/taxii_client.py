from dataclasses import dataclass
from typing import Any

import httpx

TAXII_MEDIA_TYPE = "application/taxii+json;version=2.1"
DATE_ADDED_LAST_HEADER = "X-TAXII-Date-Added-Last"


@dataclass(frozen=True)
class TaxiiPage:
    objects: list[dict[str, Any]]
    more: bool
    next: str | None
    date_added_last: str | None


class TaxiiClient:
    """Minimal synchronous TAXII 2.1 client for reading one collection."""

    def __init__(
        self,
        http: httpx.Client,
        api_root: str,
        collection_id: str,
        auth: tuple[str, str] | None = None,
    ) -> None:
        self._http = http
        self._objects_url = (
            f"{api_root.rstrip('/')}/collections/{collection_id}/objects/"
        )
        self._auth = auth

    def get_objects(
        self, added_after: str | None = None, next_token: str | None = None
    ) -> TaxiiPage:
        """Fetch one page of objects. Raises ``httpx.HTTPStatusError`` on 4xx/5xx."""
        params: dict[str, str] = {}
        if added_after:
            params["added_after"] = added_after
        if next_token:
            params["next"] = next_token
        response = self._http.get(
            self._objects_url,
            params=params,
            headers={"Accept": TAXII_MEDIA_TYPE},
            auth=self._auth,
        )
        response.raise_for_status()
        # A TAXII envelope with no objects may legitimately be an empty body.
        body: dict[str, Any] = response.json() if response.content else {}
        return TaxiiPage(
            objects=list(body.get("objects") or []),
            more=bool(body.get("more", False)),
            next=body.get("next"),
            date_added_last=response.headers.get(DATE_ADDED_LAST_HEADER),
        )
