"""Test configuration and fixtures."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

# Force testing configuration
settings.testing = True

from src.database import get_db
from src.main import app
from src.models import Base


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Specify asyncio as the backend for anyio/pytest-asyncio."""
    return "asyncio"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_test_db() -> None:
    """Creates database tables, ensuring clean schema sync by dropping first."""
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_engine():
    """Provides a fresh database engine per test to isolate event loops."""
    engine = create_async_engine(settings.database_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_db(db_engine) -> None:
    """Truncates all tables before each test using the test's engine."""
    session_local = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_local() as session:
        async with session.begin():
            await session.execute(
                text(
                    "TRUNCATE TABLE ledger, inventory, claimed_rewards, wallets, idempotency_keys CASCADE;"
                )
            )


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncSession:
    """Provides a clean database session per test for assertions."""
    session_local = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_local() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine) -> AsyncClient:
    """Provides an HTTPX AsyncClient with overridden database sessionmaker."""
    session_local = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def override_get_db():
        async with session_local() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
