from fastapi import FastAPI
from fastapi.testclient import TestClient

from database.repositories.misp_sync import InMemoryMispSyncRepository
from dependencies import get_misp_sync_repo
from routes.misp_sync import misp_sync_router

EVENT = "5e8d5c5a-0b1c-4b8e-9d2a-1a2b3c4d5e6f"


def _client() -> TestClient:
    repo = InMemoryMispSyncRepository()
    app = FastAPI()
    app.include_router(misp_sync_router)
    app.dependency_overrides[get_misp_sync_repo] = lambda: repo
    return TestClient(app)


def _lookup(client: TestClient, connector: str) -> list[dict[str, object]]:
    resp = client.post(
        f"/misp-sync/{connector}/events/lookup", json={"event_uuids": [EVENT]}
    )
    events: list[dict[str, object]] = resp.json()["events"]
    return events


def test_records_are_stored_per_connector() -> None:
    client = _client()

    put = client.put(
        f"/misp-sync/circl/events/{EVENT}",
        json={"origin": "stixhub", "misp_timestamp": 1790000000},
    )

    assert put.status_code == 200
    assert _lookup(client, "circl") == [
        {
            "connector": "circl",
            "event_uuid": EVENT,
            "origin": "stixhub",
            "misp_timestamp": 1790000000,
        }
    ]
    assert _lookup(client, "other") == []


def test_ownership_moves_to_misp_but_never_back() -> None:
    client = _client()
    url = f"/misp-sync/circl/events/{EVENT}"
    client.put(url, json={"origin": "stixhub", "misp_timestamp": None})

    assert (
        client.put(url, json={"origin": "misp", "misp_timestamp": 2}).status_code == 200
    )
    assert (
        client.put(url, json={"origin": "stixhub", "misp_timestamp": 3}).status_code
        == 409
    )
    assert _lookup(client, "circl")[0]["origin"] == "misp"
