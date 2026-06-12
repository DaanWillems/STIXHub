from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from dependencies import get_bucket_repo
from models.domain import Bucket, BucketMode, CollectionConfig, StixEntity, TaxiiCollectionModel
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


# --- Read endpoint tests ---

async def test_unknown_collection_returns_404(readable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(readable_repo)), base_url="http://test") as client:
        response = await client.get("/taxii2/root/collections/does-not-exist/objects/")

    assert response.status_code == 404
    body = response.json()
    assert body["title"] == "Collection Not Found"
    assert body["http_status"] == "404"


async def test_unreadable_collection_returns_403(readable_repo: InMemoryBucketRepository) -> None:
    unreadable_id = "unreadable-collection-id"

    def get_unreadable_collections() -> dict:  # type: ignore[type-arg]
        return {
            unreadable_id: CollectionConfig(
                taxii_collection=TaxiiCollectionModel(
                    id=unreadable_id,
                    title="Example collection",
                    description="test",
                    can_read=False,
                    can_write=True,
                    media_types=[],
                ),
                bucket_name="Example collection",
                mode=BucketMode.append,
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


# --- Write endpoint tests ---

@pytest.fixture
async def writable_repo() -> AsyncGenerator[InMemoryBucketRepository, None]:
    yield await _repo_with_bucket("Example collection", n_entities=0)


def _bundle(*objects: dict) -> dict:  # type: ignore[type-arg]
    return {"type": "bundle", "id": "bundle--test", "objects": list(objects)}


def _ipv4(value: str = "198.51.100.1") -> dict:  # type: ignore[type-arg]
    return {
        "type": "ipv4-addr",
        "id": f"ipv4-addr--original-{value}",
        "spec_version": "2.1",
        "value": value,
    }


async def test_write_unknown_collection_returns_404(writable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(writable_repo)), base_url="http://test") as client:
        response = await client.post("/taxii2/root/collections/no-such-id/objects/", json=_bundle(_ipv4()))

    assert response.status_code == 404
    assert response.json()["title"] == "Collection Not Found"


async def test_write_non_writable_collection_returns_403(writable_repo: InMemoryBucketRepository) -> None:
    readonly_id = "readonly-collection-id"

    def get_readonly() -> dict:  # type: ignore[type-arg]
        return {
            readonly_id: CollectionConfig(
                taxii_collection=TaxiiCollectionModel(
                    id=readonly_id,
                    title="Example collection",
                    description="test",
                    can_read=True,
                    can_write=False,
                    media_types=[],
                ),
                bucket_name="Example collection",
                mode=BucketMode.append,
            )
        }

    app = make_app(writable_repo)
    app.dependency_overrides[get_dummy_collections] = get_readonly

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/taxii2/root/collections/{readonly_id}/objects/", json=_bundle(_ipv4()))

    assert response.status_code == 403
    assert response.json()["title"] == "Forbidden"


async def test_write_valid_object_returns_202_complete(writable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(writable_repo)), base_url="http://test") as client:
        response = await client.post(OBJECTS_URL, json=_bundle(_ipv4()))

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "complete"
    assert body["total_count"] == 1
    assert body["success_count"] == 1
    assert body["failure_count"] == 0
    assert len(body["successes"]) == 1


async def test_write_stores_object_in_bucket(writable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(writable_repo)), base_url="http://test") as client:
        await client.post(OBJECTS_URL, json=_bundle(_ipv4("10.0.0.1")))

    entities = await writable_repo.get_entities(bucket_name="Example collection")
    assert len(entities) == 1
    assert entities[0].object["value"] == "10.0.0.1"
    assert entities[0].creator == COLLECTION_ID
    assert entities[0].other_stix_ids == ["ipv4-addr--original-10.0.0.1"]


async def test_write_generates_deterministic_platform_id(writable_repo: InMemoryBucketRepository) -> None:
    async with AsyncClient(transport=ASGITransport(app=make_app(writable_repo)), base_url="http://test") as client:
        await client.post(OBJECTS_URL, json=_bundle(_ipv4("10.0.0.1")))
        await client.post(OBJECTS_URL, json=_bundle(_ipv4("10.0.0.1")))

    entities = await writable_repo.get_entities(bucket_name="Example collection")
    assert entities[0].stix_id == entities[1].stix_id


async def test_write_missing_required_field_is_failure(writable_repo: InMemoryBucketRepository) -> None:
    bad_obj = {"type": "ipv4-addr", "spec_version": "2.1", "value": "1.2.3.4"}  # missing id

    async with AsyncClient(transport=ASGITransport(app=make_app(writable_repo)), base_url="http://test") as client:
        response = await client.post(OBJECTS_URL, json=_bundle(bad_obj))

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "complete_with_errors"
    assert body["success_count"] == 0
    assert body["failure_count"] == 1


async def test_write_unimplemented_type_is_failure(writable_repo: InMemoryBucketRepository) -> None:
    sro = {
        "type": "relationship",
        "id": "relationship--abc",
        "spec_version": "2.1",
        "relationship_type": "uses",
        "source_ref": "malware--x",
        "target_ref": "tool--y",
    }

    async with AsyncClient(transport=ASGITransport(app=make_app(writable_repo)), base_url="http://test") as client:
        response = await client.post(OBJECTS_URL, json=_bundle(sro))

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "complete_with_errors"
    assert body["failure_count"] == 1
    assert "relationship" in body["failures"][0]["message"]


async def test_write_partial_success(writable_repo: InMemoryBucketRepository) -> None:
    bad_obj = {"type": "ipv4-addr", "spec_version": "2.1", "value": "1.2.3.4"}  # missing id

    async with AsyncClient(transport=ASGITransport(app=make_app(writable_repo)), base_url="http://test") as client:
        response = await client.post(OBJECTS_URL, json=_bundle(_ipv4("5.6.7.8"), bad_obj))

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "complete_with_errors"
    assert body["total_count"] == 2
    assert body["success_count"] == 1
    assert body["failure_count"] == 1

    entities = await writable_repo.get_entities(bucket_name="Example collection")
    assert len(entities) == 1
