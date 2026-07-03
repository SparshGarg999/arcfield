"""Database connection and session management."""

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

# Create async engine
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    echo=False,  # Set to True for SQL query logging if needed
)

# Async session maker
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for obtaining a database session."""
    async with AsyncSessionLocal() as session:
        yield session
