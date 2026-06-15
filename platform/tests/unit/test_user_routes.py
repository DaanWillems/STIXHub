from typing import AsyncGenerator

from config import load_platform_config
import pytest
from httpx import ASGITransport, AsyncClient

from dependencies import get_user_repo, require_admin
from repositories.user import InMemoryUserRepository, UserRepository
from routes.users import users_router

from fastapi import FastAPI


def make_app(repo: UserRepository) -> FastAPI:
    app = FastAPI()
    app.include_router(users_router)
    app.dependency_overrides[get_user_repo] = lambda: repo
    app.dependency_overrides[require_admin] = lambda: None
    config = load_platform_config()
    app.state.platform_config = config
    return app


@pytest.fixture
async def repo() -> AsyncGenerator[InMemoryUserRepository, None]:
    yield InMemoryUserRepository()


# --- Create user ---


async def test_create_user_returns_201_with_api_key(
    repo: InMemoryUserRepository,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(repo)), base_url="http://test"
    ) as client:
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


async def test_create_user_duplicate_email_returns_409(
    repo: InMemoryUserRepository,
) -> None:
    app = make_app(repo)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
        )
        response = await client.post(
            "/users/", json={"email": "alice@example.com", "roles": ["reader"]}
        )

    assert response.status_code == 409


async def test_create_user_unknown_role_returns_422(
    repo: InMemoryUserRepository,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(repo)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/users/", json={"email": "bob@example.com", "roles": ["nonexistent-role"]}
        )

    assert response.status_code == 422


async def test_list_users_returns_empty_list(repo: InMemoryUserRepository) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=make_app(repo)), base_url="http://test"
    ) as client:
        response = await client.get("/users/")

    assert response.status_code == 200
    assert response.json() == []


async def test_list_users_returns_created_users(repo: InMemoryUserRepository) -> None:
    app = make_app(repo)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/users/", json={"email": "a@example.com", "roles": ["admin"]}
        )
        await client.post(
            "/users/", json={"email": "b@example.com", "roles": ["reader"]}
        )
        response = await client.get("/users/")

    assert response.status_code == 200
    emails = {u["email"] for u in response.json()}
    assert emails == {"a@example.com", "b@example.com"}
    assert all("api_key" not in u for u in response.json())


# --- Patch user ---


async def test_patch_user_updates_roles(repo: InMemoryUserRepository) -> None:
    app = make_app(repo)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post(
            "/users/", json={"email": "alice@example.com", "roles": ["reader"]}
        )
        user_id = create_resp.json()["id"]
        response = await client.patch(f"/users/{user_id}", json={"roles": ["admin"]})

    assert response.status_code == 200
    assert response.json()["roles"] == ["admin"]


async def test_patch_user_not_found_returns_404(repo: InMemoryUserRepository) -> None:
    import uuid

    async with AsyncClient(
        transport=ASGITransport(app=make_app(repo)), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/users/{uuid.uuid4()}", json={"roles": ["admin"]}
        )

    assert response.status_code == 404


async def test_patch_user_unknown_role_returns_422(
    repo: InMemoryUserRepository,
) -> None:
    app = make_app(repo)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post(
            "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
        )
        user_id = create_resp.json()["id"]
        response = await client.patch(f"/users/{user_id}", json={"roles": ["ghost"]})

    assert response.status_code == 422


# --- Delete user ---


async def test_delete_user_returns_204(repo: InMemoryUserRepository) -> None:
    app = make_app(repo)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post(
            "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
        )
        user_id = create_resp.json()["id"]
        response = await client.delete(f"/users/{user_id}")

    assert response.status_code == 204


async def test_delete_user_not_found_returns_404(repo: InMemoryUserRepository) -> None:
    import uuid

    async with AsyncClient(
        transport=ASGITransport(app=make_app(repo)), base_url="http://test"
    ) as client:
        response = await client.delete(f"/users/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_delete_user_removes_from_list(repo: InMemoryUserRepository) -> None:
    app = make_app(repo)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post(
            "/users/", json={"email": "alice@example.com", "roles": ["admin"]}
        )
        user_id = create_resp.json()["id"]
        await client.delete(f"/users/{user_id}")
        response = await client.get("/users/")

    assert response.status_code == 200
    assert response.json() == []
