import logging
from collections.abc import AsyncGenerator

from fastapi.concurrency import asynccontextmanager
from sqlalchemy import URL, NullPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from server.config import settings
from shared_models.data import Base

logger = logging.getLogger(__name__)


class Database:
    def __init__(self) -> None:
        if settings.DATABASE_USE_NULLPOOL:
            self._engine = create_async_engine(
                self._create_url(),
                echo=settings.DATABASE_ENGINE_ECHO,
                poolclass=NullPool,
                connect_args={"timeout": 1},
            )
        else:
            self._engine = create_async_engine(
                self._create_url(),
                echo=settings.DATABASE_ENGINE_ECHO,
                pool_size=5,  # number of persistent connections
                max_overflow=10,  # extra connections allowed under load
                pool_timeout=30,  # seconds to wait for a connection before raising
                pool_recycle=1800,  # recycle connections after 30 min to avoid stale ones
                connect_args={"timeout": 1},
            )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    @staticmethod
    def _create_url() -> URL:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=settings.DATABASE_USER,
            password=settings.DATABASE_PASS,
            host=settings.DATABASE_HOST,
            port=settings.DATABASE_PORT,
            database=settings.DATABASE_NAME,
        )

    def get_engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession]:
        async with self._session_factory() as session:
            async with session.begin():
                yield session

    async def create_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()


db = Database()


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with db.get_session() as session:
        yield session
