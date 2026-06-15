from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config import load_platform_config, settings
from dependencies import get_bucket_repo, get_user_repo
from models.domain import Bucket, StixEntity, TaxiiCollectionModel
from models.domain import CollectionConfig
from repositories.bucket import DatabaseBucketRepository
from repositories.user import DatabaseUserRepository
from routes.taxii2 import taxii2_router
from routes.users import users_router


COLLECTION_ID = "70a16fcf-8146-2da8-be66-6ca6fb7280af"
OBJECTS_URL = f"/taxii2/root/collections/{COLLECTION_ID}/objects/"

_DEFAULT_COLLECTION = CollectionConfig(
    taxii_collection=TaxiiCollectionModel(
        id=COLLECTION_ID,
        title="Example collection",
        description="test",
        can_read=True,
        can_write=True,
    ),
    bucket_name="Example collection",
)


def make_app(
    bucket_repo: DatabaseBucketRepository, user_repo: DatabaseUserRepository
) -> FastAPI:
    app = FastAPI()
    app.include_router(taxii2_router)
    app.include_router(users_router)
    app.dependency_overrides[get_bucket_repo] = lambda: bucket_repo
    app.dependency_overrides[get_user_repo] = lambda: user_repo
    app.state.active_collections = {COLLECTION_ID: _DEFAULT_COLLECTION}

    config = load_platform_config()
    app.state.platform_config = config
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
async def api_key(
    bucket_repo: DatabaseBucketRepository, user_repo: DatabaseUserRepository
) -> str:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(bucket_repo, user_repo)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.ADMIN_API_KEY}"},
    ) as admin_client:
        response = await admin_client.post(
            "/users/", json={"email": "test@example.com", "roles": ["admin"]}
        )
    assert response.status_code == 201
    return str(response.json()["api_key"])


@pytest.fixture
async def bucket(bucket_repo: DatabaseBucketRepository) -> Bucket:
    return await bucket_repo.save(Bucket(name="Example collection"))


@pytest.fixture
async def client(
    bucket_repo: DatabaseBucketRepository,
    user_repo: DatabaseUserRepository,
    api_key: str,
) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(bucket_repo, user_repo)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {api_key}"},
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
    client: AsyncClient, bucket_repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    other_bucket = await bucket_repo.save(Bucket(name="other-bucket"))
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, "indicator--correct")]
    )
    await bucket_repo.add_entities(
        other_bucket.id, [make_entity(other_bucket.id, "indicator--wrong")]
    )

    response = await client.get(OBJECTS_URL)

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) == 1
    assert body["objects"][0]["id"] == "indicator--correct"


async def test_returns_all_objects_when_under_limit(
    client: AsyncClient, bucket_repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(3)]
    )

    response = await client.get(OBJECTS_URL, params={"limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) == 3
    assert body["more"] is False
    assert "next" not in body


async def test_pagination_sets_more_and_next_cursor(
    client: AsyncClient, bucket_repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    await bucket_repo.add_entities(
        bucket.id, [make_entity(bucket.id, f"indicator--{i}") for i in range(5)]
    )

    response = await client.get(OBJECTS_URL, params={"limit": 3})

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) == 3
    assert body["more"] is True
    assert "next" in body


async def test_cursor_advances_to_next_page(
    client: AsyncClient, bucket_repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    await bucket_repo.add_entities(
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
    client: AsyncClient, bucket_repo: DatabaseBucketRepository, bucket: Bucket
) -> None:
    await bucket_repo.add_entities(
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


async def test_unauthenticated_request_returns_401(
    bucket_repo: DatabaseBucketRepository,
    user_repo: DatabaseUserRepository,
    bucket: Bucket,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(bucket_repo, user_repo)),
        base_url="http://test",
    ) as unauthenticated:
        response = await unauthenticated.get(OBJECTS_URL)

    assert response.status_code == 401


async def test_invalid_token_returns_401(
    bucket_repo: DatabaseBucketRepository,
    user_repo: DatabaseUserRepository,
    bucket: Bucket,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(bucket_repo, user_repo)),
        base_url="http://test",
        headers={"Authorization": "Bearer not-a-real-key"},
    ) as bad_client:
        response = await bad_client.get(OBJECTS_URL)

    assert response.status_code == 401
