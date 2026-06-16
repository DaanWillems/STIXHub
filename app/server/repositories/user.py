from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import UserModel
from server.models.domain import User


def _user_from_model(model: UserModel) -> User:
    return User(
        id=model.id,
        email=model.email,
        roles=list(model.roles),
        created_at=model.created_at,
    )


class UserRepository(ABC):
    @abstractmethod
    async def get_by_email(self, email: str) -> User | None: ...

    @abstractmethod
    async def get_by_id(self, user_id: UUID) -> User | None: ...

    @abstractmethod
    async def get_by_key_hash(self, key_hash: str) -> User | None: ...

    @abstractmethod
    async def list_all(self) -> list[User]: ...

    @abstractmethod
    async def save(self, user: User, api_key_hash: str) -> User: ...

    @abstractmethod
    async def update_roles(self, user_id: UUID, roles: list[str]) -> User: ...

    @abstractmethod
    async def delete(self, user_id: UUID) -> None: ...


class InMemoryUserRepository(UserRepository):
    def __init__(self) -> None:
        self._users: dict[UUID, User] = {}
        self._email_index: dict[str, UUID] = {}
        self._key_hash_index: dict[str, UUID] = {}

    async def get_by_email(self, email: str) -> User | None:
        user_id = self._email_index.get(email)
        return self._users.get(user_id) if user_id is not None else None

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._users.get(user_id)

    async def get_by_key_hash(self, key_hash: str) -> User | None:
        user_id = self._key_hash_index.get(key_hash)
        return self._users.get(user_id) if user_id is not None else None

    async def list_all(self) -> list[User]:
        return list(self._users.values())

    async def save(self, user: User, api_key_hash: str) -> User:
        self._users[user.id] = user
        self._email_index[user.email] = user.id
        self._key_hash_index[api_key_hash] = user.id
        return user

    async def update_roles(self, user_id: UUID, roles: list[str]) -> User:
        existing = self._users[user_id]
        updated = User(
            id=existing.id,
            email=existing.email,
            roles=roles,
            created_at=existing.created_at,
        )
        self._users[user_id] = updated
        return updated

    async def delete(self, user_id: UUID) -> None:
        user = self._users.pop(user_id)
        self._email_index.pop(user.email, None)
        stale = [h for h, uid in self._key_hash_index.items() if uid == user_id]
        for h in stale:
            del self._key_hash_index[h]


class DatabaseUserRepository(UserRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            select(UserModel).where(UserModel.email == email)
        )
        model = result.scalar_one_or_none()
        return _user_from_model(model) if model is not None else None

    async def get_by_id(self, user_id: UUID) -> User | None:
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user_id)
        )
        model = result.scalar_one_or_none()
        return _user_from_model(model) if model is not None else None

    async def get_by_key_hash(self, key_hash: str) -> User | None:
        result = await self._session.execute(
            select(UserModel).where(UserModel.api_key_hash == key_hash)
        )
        model = result.scalar_one_or_none()
        return _user_from_model(model) if model is not None else None

    async def list_all(self) -> list[User]:
        result = await self._session.execute(
            select(UserModel).order_by(UserModel.created_at)
        )
        return [_user_from_model(m) for m in result.scalars().all()]

    async def save(self, user: User, api_key_hash: str) -> User:
        model = UserModel(
            id=user.id,
            email=user.email,
            roles=user.roles,
            api_key_hash=api_key_hash,
            created_at=user.created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _user_from_model(model)

    async def update_roles(self, user_id: UUID, roles: list[str]) -> User:
        await self._session.execute(
            update(UserModel).where(UserModel.id == user_id).values(roles=roles)
        )
        await self._session.flush()
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user_id)
        )
        return _user_from_model(result.scalar_one())

    async def delete(self, user_id: UUID) -> None:
        await self._session.execute(delete(UserModel).where(UserModel.id == user_id))
        await self._session.flush()
