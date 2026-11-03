from fastapi import FastAPI
from fastapi.testclient import TestClient

from dependencies import get_bucket_repo, get_collector_repo
from models.domain import Bucket
from database.repositories.bucket import InMemoryBucketRepository
from database.repositories.collector import InMemoryCollectorRepository
from routes.collectors import collectors_router

BUCKET = "intel"

INDICATOR = {
    "type": "indicator",
    "spec_version": "2.1",
    "id": "indicator--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f",
    "pattern": "[ipv4-addr:value = '198.51.100.1']",
    "pattern_type": "stix",
    "valid_from": "2026-01-01T00:00:00Z",
}


async def _client() -> tuple[TestClient, InMemoryBucketRepository]:
    bucket_repo = InMemoryBucketRepository()
    await bucket_repo.save(Bucket(name=BUCKET))
    collector_repo = InMemoryCollectorRepository()

    app = FastAPI()
    app.include_router(collectors_router)
    app.dependency_overrides[get_bucket_repo] = lambda: bucket_repo
    app.dependency_overrides[get_collector_repo] = lambda: collector_repo
    return TestClient(app), bucket_repo


async def test_register_new_collector_returns_empty_cursor() -> None:
    client, _ = await _client()
    resp = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": BUCKET}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "c"
    assert body["cursor"] is None


async def test_register_unknown_bucket_returns_404() -> None:
    client, _ = await _client()
    resp = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": "missing"}
    )
    assert resp.status_code == 404
    assert "missing" in resp.json()["detail"]


async def test_register_again_returns_same_id_and_stored_cursor() -> None:
    client, _ = await _client()
    first = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": BUCKET}
    ).json()
    put = client.put(
        f"/collectors/{first['id']}/cursor", json={"cursor": "2026-02-01T00:00:00Z"}
    )
    assert put.status_code == 200

    again = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": BUCKET}
    ).json()
    assert again["id"] == first["id"]
    assert again["cursor"] == "2026-02-01T00:00:00Z"


async def test_submit_objects_stores_in_bucket_and_reports_failures() -> None:
    client, bucket_repo = await _client()
    collector = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": BUCKET}
    ).json()

    bad = {"type": "indicator", "id": "indicator--bad"}  # no spec_version
    resp = client.post(
        f"/collectors/{collector['id']}/objects", json={"objects": [INDICATOR, bad]}
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "complete_with_errors"
    assert body["success_count"] == 1
    assert body["failures"][0]["id"] == "indicator--bad"
    entities = await bucket_repo.get_entities(bucket_name=BUCKET)
    assert len(entities) == 1
    assert entities[0].creator == "collector:c"
    assert entities[0].other_stix_ids == [INDICATOR["id"]]


async def test_unknown_collector_returns_404() -> None:
    client, _ = await _client()
    assert (
        client.post("/collectors/99/objects", json={"objects": []}).status_code == 404
    )
    assert client.put("/collectors/99/cursor", json={"cursor": "x"}).status_code == 404


def _indicator(original_id: str, ip: str) -> dict[str, object]:
    return {**INDICATOR, "id": original_id, "pattern": f"[ipv4-addr:value = '{ip}']"}


async def test_entities_are_paged_by_id() -> None:
    client, _ = await _client()
    collector = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": BUCKET}
    ).json()
    objects = [_indicator(f"indicator--{n}", f"198.51.100.{n}") for n in range(3)]
    client.post(f"/collectors/{collector['id']}/objects", json={"objects": objects})
    url = f"/collectors/{collector['id']}/entities"

    first = client.get(url, params={"limit": 2}).json()["entities"]
    rest = client.get(url, params={"after": first[-1]["id"]}).json()["entities"]

    assert [e["other_stix_ids"] for e in first + rest] == [
        ["indicator--0"],
        ["indicator--1"],
        ["indicator--2"],
    ]
    assert first[0]["creator"] == "collector:c"


async def test_lookup_finds_newest_entity_by_platform_or_original_id() -> None:
    client, _ = await _client()
    collector = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": BUCKET}
    ).json()
    submit = f"/collectors/{collector['id']}/objects"
    old = client.post(submit, json={"objects": [_indicator("indicator--a", "1.1.1.1")]})
    client.post(submit, json={"objects": [_indicator("indicator--b", "1.1.1.1")]})
    platform_id = old.json()["successes"][0]["id"]

    resp = client.post(
        f"/collectors/{collector['id']}/entities/lookup",
        json={"refs": ["indicator--a", platform_id, "indicator--unknown"]},
    )

    found = resp.json()["entities"]
    assert set(found) == {"indicator--a", platform_id}
    assert found["indicator--a"]["other_stix_ids"] == ["indicator--a"]
    # Both entities share the platform ID; the newest wins.
    assert found[platform_id]["other_stix_ids"] == ["indicator--b"]
