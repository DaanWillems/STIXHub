from fastapi import FastAPI
from fastapi.testclient import TestClient

from pydantic import SecretStr

from dependencies import (
    get_bucket_repo,
    get_collector_repo,
    get_collector_secrets,
    get_platform_config,
)
from models.domain import Bucket, CollectorConfig, PlatformConfig
from database.repositories.bucket import InMemoryBucketRepository
from database.repositories.collector import InMemoryCollectorRepository
from routes.collectors import collectors_router

BUCKET = "intel"
SECRETS = {"c": "secret-c", "other": "secret-other"}
AUTH = {"Authorization": "Bearer secret-c"}
OTHER_AUTH = {"Authorization": "Bearer secret-other"}

INDICATOR = {
    "type": "indicator",
    "spec_version": "2.1",
    "id": "indicator--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f",
    "pattern": "[ipv4-addr:value = '198.51.100.1']",
    "pattern_type": "stix",
    "valid_from": "2026-01-01T00:00:00Z",
}


def _collector_config(name: str) -> CollectorConfig:
    return CollectorConfig(
        name=name,
        remote_url="https://taxii.example.com/api/",
        remote_collection_id="coll",
        target_bucket=BUCKET,
        secret_env=name.upper(),
    )


async def _client() -> tuple[TestClient, InMemoryBucketRepository]:
    bucket_repo = InMemoryBucketRepository()
    await bucket_repo.save(Bucket(name=BUCKET))
    collector_repo = InMemoryCollectorRepository()

    app = FastAPI()
    app.include_router(collectors_router)
    app.dependency_overrides[get_bucket_repo] = lambda: bucket_repo
    app.dependency_overrides[get_collector_repo] = lambda: collector_repo
    app.dependency_overrides[get_platform_config] = lambda: PlatformConfig(
        buckets=[],
        collections=[],
        roles=[],
        pipelines=[],
        collectors=[_collector_config(name) for name in SECRETS],
    )
    app.dependency_overrides[get_collector_secrets] = lambda: {
        name: SecretStr(secret) for name, secret in SECRETS.items()
    }
    return TestClient(app, headers=AUTH), bucket_repo


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


async def test_missing_or_wrong_secret_returns_401() -> None:
    client, _ = await _client()
    body = {"name": "c", "bucket_name": BUCKET}
    no_auth = TestClient(client.app)
    assert no_auth.post("/collectors/register", json=body).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (
        client.post("/collectors/register", json=body, headers=wrong).status_code
        == 401
    )
    assert no_auth.post("/collectors/1/objects", json={"objects": []}).status_code == (
        401
    )
    assert no_auth.put("/collectors/1/cursor", json={"cursor": "x"}).status_code == 401


async def test_register_under_another_collectors_name_returns_403() -> None:
    client, _ = await _client()
    resp = client.post(
        "/collectors/register",
        json={"name": "other", "bucket_name": BUCKET},
    )
    assert resp.status_code == 403


async def test_collector_cannot_act_on_another_collector() -> None:
    client, _ = await _client()
    collector = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": BUCKET}
    ).json()

    objects = client.post(
        f"/collectors/{collector['id']}/objects",
        json={"objects": [INDICATOR]},
        headers=OTHER_AUTH,
    )
    cursor = client.put(
        f"/collectors/{collector['id']}/cursor",
        json={"cursor": "2026-02-01T00:00:00Z"},
        headers=OTHER_AUTH,
    )

    assert objects.status_code == 403
    assert cursor.status_code == 403
    again = client.post(
        "/collectors/register", json={"name": "c", "bucket_name": BUCKET}
    ).json()
    assert again["cursor"] is None


async def test_unconfigured_secret_authenticates_nobody() -> None:
    client, _ = await _client()
    client.app.dependency_overrides[get_collector_secrets] = lambda: {
        "c": SecretStr("")
    }
    resp = client.post(
        "/collectors/register",
        json={"name": "c", "bucket_name": BUCKET},
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code == 401
