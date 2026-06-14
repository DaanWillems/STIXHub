import pytest
from repositories.user import DatabaseUserRepository
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from models.data import Base
from repositories.bucket import DatabaseBucketRepository


DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/app"


@pytest.fixture(scope="session")
async def engine():
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def session(engine):
    async with engine.connect() as conn:
        await conn.begin()
        sess = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        yield sess
        await sess.close()
        await conn.rollback()


@pytest.fixture
def bucket_repo(session: AsyncSession) -> DatabaseBucketRepository:
    return DatabaseBucketRepository(session)


@pytest.fixture
def user_repo(session: AsyncSession) -> DatabaseUserRepository:
    return DatabaseUserRepository(session)
