from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dependencies import get_bucket_repo
from models.domain import Bucket, CollectionConfig, StixEntity
from repositories.bucket import DatabaseBucketRepository
from routes.taxii2 import taxii2_router


COLLECTION_ID = "70a16fcf-8146-2da8-be66-6ca6fb7280af"
OBJECTS_URL = f"/taxii2/root/collections/{COLLECTION_ID}/objects/"

_DEFAULT_COLLECTION = CollectionConfig(
    id=COLLECTION_ID,
    title="Example collection",
    description="test",
    can_read=True,
    can_write=True,
    bucket_name="Example collection",
)


def make_app(repo: DatabaseBucketRepository) -> FastAPI:
    app = FastAPI()
    app.include_router(taxii2_router)
    app.dependency_overrides[get_bucket_repo] = lambda: repo
    app.state.active_collections = {COLLECTION_ID: _DEFAULT_COLLECTION}
    return app


def make_entity(bucket_id: int, stix_id: str = "indicator--abc123") -> StixEntity:
    now = datetime.now(timezone.utc)
    return StixEntity(
        id=0,
        bucket_id=bucket_id,
        stix_id=stix_id,
        type="indicator",
        spec_version="2.1",
        creator="test",
        value="test-value",
        platform_modified=now,
        platform_created=now,
        object={"id": stix_id, "type": "indicator"},
    )


@pytest.fixture
async def bucket(repo: DatabaseBucketRepository) -> Bucket:
    return await repo.save(Bucket(name="Example collection"))


@pytest.fixture
async def client(repo: DatabaseBucketRepository) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(repo)), base_url="http://test"
    ) as ac:
        yield ac


async def test_empty_bucket_returns_empty_objects(
    client: AsyncClient, bucket: Bucket
) -> None:
    response = await client.get(OBJECTS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["objects"] == []
    assert body["more"] is False
    assert "next" not in body


async def test_returns_objects_from_correct_bucket(
    client: AsyncClient, repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    other_bucket = await repo.save(Bucket(name="other-bucket"))
    await repo.add_entities(bucket.id, [make_entity(bucket.id, "indicator--correct")])
    await repo.add_entities(
        other_bucket.id, [make_entity(other_bucket.id, "indicator--wrong")]
    )

    response = await client.get(OBJECTS_URL)

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) == 1
    assert body["objects"][0]["id"] == "indicator--correct"


async def test_returns_all_objects_when_under_limit(
    client: AsyncClient, repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(3)]
    )

    response = await client.get(OBJECTS_URL, params={"limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) == 3
    assert body["more"] is False
    assert "next" not in body


async def test_pagination_sets_more_and_next_cursor(
    client: AsyncClient, repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    response = await client.get(OBJECTS_URL, params={"limit": 3})

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) == 3
    assert body["more"] is True
    assert "next" in body


async def test_cursor_advances_to_next_page(
    client: AsyncClient, repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i:04d}") for i in range(5)]
    )

    first = await client.get(OBJECTS_URL, params={"limit": 3})
    second = await client.get(
        OBJECTS_URL, params={"limit": 3, "next": first.json()["next"]}
    )

    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["objects"]) == 2
    assert second_body["more"] is False
    assert "next" not in second_body

    first_ids = {o["id"] for o in first.json()["objects"]}
    second_ids = {o["id"] for o in second_body["objects"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 5


async def test_pages_are_stable_and_ordered(
    client: AsyncClient, repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    await repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i:04d}") for i in range(6)]
    )

    all_ids: list[str] = []
    cursor = None
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor:
            params["next"] = cursor
        body = (await client.get(OBJECTS_URL, params=params)).json()
        all_ids.extend(o["id"] for o in body["objects"])
        if not body["more"]:
            break
        cursor = body["next"]

    assert len(all_ids) == 6
    assert all_ids == sorted(all_ids)


async def test_unknown_collection_returns_404(client: AsyncClient) -> None:
    response = await client.get("/taxii2/root/collections/does-not-exist/objects/")

    assert response.status_code == 404
    body = response.json()
    assert body["title"] == "Collection Not Found"
    assert body["http_status"] == "404"
