from collections.abc import AsyncGenerator

from config import load_platform_config
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies import get_user_repo, require_admin
from models.models import UserModel
from database.repositories.user import DatabaseUserRepository
from routes.users import users_router


def make_app(repo: DatabaseUserRepository) -> FastAPI:
    app = FastAPI()
    app.include_router(users_router)
    app.dependency_overrides[get_user_repo] = lambda: repo
    app.dependency_overrides[require_admin] = lambda: None
    config = load_platform_config()
    app.state.platform_config = config
    return app


@pytest.fixture
def user_repo(session: AsyncSession) -> DatabaseUserRepository:
    return DatabaseUserRepository(session)


@pytest.fixture
async def client(
    user_repo: DatabaseUserRepository,
) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(user_repo)), base_url="http://test"
    ) as ac:
        yield ac


# --- Create user ---


async def test_create_user_returns_201_with_api_key(client: AsyncClient) -> None:
    response = await client.post(
        "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["roles"] == ["admin"]
    assert "id" in body
    assert "created_at" in body
    assert "api_key" in body
    assert len(body["api_key"]) == 64


async def test_create_user_duplicate_email_returns_409(client: AsyncClient) -> None:
    await client.post(
        "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
    )
    response = await client.post(
        "/users/", json={"email": "alice@example.com", "roles": ["reader"]}
    )

    assert response.status_code == 409


async def test_api_key_hash_stored_not_plaintext(
    client: AsyncClient, session: AsyncSession
) -> None:
    response = await client.post(
        "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
    )
    api_key = response.json()["api_key"]

    result = await session.execute(
        select(UserModel).where(UserModel.email == "alice@example.com")
    )
    model = result.scalar_one()

    assert model.api_key_hash != api_key
    assert len(model.api_key_hash) == 64


# --- List users ---


async def test_list_users_returns_empty_list(client: AsyncClient) -> None:
    response = await client.get("/users/")

    assert response.status_code == 200
    assert response.json() == []


async def test_list_users_returns_created_users(client: AsyncClient) -> None:
    await client.post("/users/", json={"email": "a@example.com", "roles": ["admin"]})
    await client.post("/users/", json={"email": "b@example.com", "roles": ["reader"]})

    response = await client.get("/users/")

    assert response.status_code == 200
    emails = {u["email"] for u in response.json()}
    assert emails == {"a@example.com", "b@example.com"}
    assert all("api_key" not in u for u in response.json())


# --- Patch user ---


async def test_patch_user_updates_roles(client: AsyncClient) -> None:
    create_resp = await client.post(
        "/users/", json={"email": "alice@example.com", "roles": ["reader"]}
    )
    user_id = create_resp.json()["id"]

    response = await client.patch(f"/users/{user_id}", json={"roles": ["admin"]})

    assert response.status_code == 200
    assert response.json()["roles"] == ["admin"]


async def test_patch_user_not_found_returns_404(client: AsyncClient) -> None:
    import uuid

    response = await client.patch(f"/users/{uuid.uuid4()}", json={"roles": ["admin"]})

    assert response.status_code == 404


# --- Delete user ---


async def test_delete_user_returns_204(client: AsyncClient) -> None:
    create_resp = await client.post(
        "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
    )
    user_id = create_resp.json()["id"]

    response = await client.delete(f"/users/{user_id}")

    assert response.status_code == 204


async def test_delete_user_not_found_returns_404(client: AsyncClient) -> None:
    import uuid

    response = await client.delete(f"/users/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_delete_user_removes_from_list(client: AsyncClient) -> None:
    create_resp = await client.post(
        "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
    )
    user_id = create_resp.json()["id"]
    await client.delete(f"/users/{user_id}")

    response = await client.get("/users/")

    assert response.status_code == 200
    assert response.json() == []
