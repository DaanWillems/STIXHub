from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from dependencies import get_bucket_repo
from models.domain import Bucket, StixEntity
from repositories.bucket import BucketRepository, InMemoryBucketRepository
from routes.taxii2 import get_dummy_collections, taxii2_router

from fastapi import FastAPI


def make_app(repo: BucketRepository) -> FastAPI:
    app = FastAPI()
    app.include_router(taxii2_router)
    app.dependency_overrides[get_bucket_repo] = lambda: repo
    return app


async def _repo_with_bucket(name: str, n_entities: int = 0) -> InMemoryBucketRepository:
    repo = InMemoryBucketRepository()
    bucket = await repo.save(Bucket(name=name))
    if n_entities:
        now = datetime.now(timezone.utc)
        entities = [
            StixEntity(
                id=i,
                bucket_id=bucket.id,
                stix_id=f"indicator--{i:04d}",
                type="indicator",
                spec_version="2.1",
                creator="test",
                value="v",
                platform_modified=now,
                platform_created=now,
                object={"id": f"indicator--{i:04d}", "type": "indicator"},
            )
            for i in range(n_entities)
        ]
        await repo.add_entities(bucket.id, entities)
    return repo


@pytest.fixture
async def readable_repo() -> AsyncGenerator[InMemoryBucketRepository, None]:
    yield await _repo_with_bucket("Example collection", n_entities=5)


@pytest.fixture
async def empty_repo() -> AsyncGenerator[InMemoryBucketRepository, None]:
    yield await _repo_with_bucket("Example collection", n_entities=0)


COLLECTION_ID = "70a16fcf-8146-2da8-be66-6ca6fb7280af"
OBJECTS_URL = f"/taxii2/root/collections/{COLLECTION_ID}/objects/"


async def test_unknown_collection_returns_404(readable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(readable_repo)), base_url="http://test") as client:
        response = await client.get("/taxii2/root/collections/does-not-exist/objects/")

    assert response.status_code == 404
    body = response.json()
    assert body["title"] == "Collection Not Found"
    assert body["http_status"] == "404"


async def test_unreadable_collection_returns_403(readable_repo: InMemoryBucketRepository) -> None:
    unreadable_id = "unreadable-collection-id"

    from models.domain import TaxiiCollectionModel

    def get_unreadable_collections() -> dict:  # type: ignore[type-arg]
        return {
            unreadable_id: TaxiiCollectionModel(
                id=unreadable_id,
                title="Example collection",
                description="test",
                can_read=False,
                can_write=True,
                media_types=[],
            )
        }

    app = make_app(readable_repo)
    app.dependency_overrides[get_dummy_collections] = get_unreadable_collections

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/taxii2/root/collections/{unreadable_id}/objects/")

    assert response.status_code == 403
    body = response.json()
    assert body["title"] == "Forbidden"
    assert body["http_status"] == "403"


async def test_empty_bucket_returns_empty_objects(empty_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(empty_repo)), base_url="http://test") as client:
        response = await client.get(OBJECTS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["objects"] == []
    assert body["more"] is False
    assert "next" not in body


async def test_returns_all_objects_when_under_limit(readable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(readable_repo)), base_url="http://test") as client:
        response = await client.get(OBJECTS_URL, params={"limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) == 5
    assert body["more"] is False
    assert "next" not in body


async def test_pagination_sets_more_and_next_cursor(readable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(readable_repo)), base_url="http://test") as client:
        response = await client.get(OBJECTS_URL, params={"limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) == 2
    assert body["more"] is True
    assert "next" in body


async def test_next_cursor_advances_page(readable_repo: InMemoryBucketRepository) -> None:
    app = make_app(readable_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(OBJECTS_URL, params={"limit": 2})
        cursor = first.json()["next"]
        second = await client.get(OBJECTS_URL, params={"limit": 2, "next": cursor})

    assert second.status_code == 200
    body = second.json()
    assert len(body["objects"]) == 2
    first_ids = {o["id"] for o in first.json()["objects"]}
    second_ids = {o["id"] for o in body["objects"]}
    assert first_ids.isdisjoint(second_ids)


async def test_last_page_has_no_next_cursor(readable_repo: InMemoryBucketRepository) -> None:
    app = make_app(readable_repo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(OBJECTS_URL, params={"limit": 3})
        cursor = first.json()["next"]
        last = await client.get(OBJECTS_URL, params={"limit": 3, "next": cursor})

    assert last.status_code == 200
    body = last.json()
    assert len(body["objects"]) == 2
    assert body["more"] is False
    assert "next" not in body


async def test_invalid_cursor_returns_400(readable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(readable_repo)), base_url="http://test") as client:
        response = await client.get(OBJECTS_URL, params={"next": "not-valid-base64!!"})

    assert response.status_code == 400
    body = response.json()
    assert body["title"] == "Invalid Cursor"
    assert body["http_status"] == "400"
