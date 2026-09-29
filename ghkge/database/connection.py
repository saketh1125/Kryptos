from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from ghkge.config.settings import settings

logger = structlog.get_logger()

engine: AsyncEngine | None = None
async_session_factory: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    pass


async def init_db() -> AsyncEngine:
    """Initialize the async database engine and session factory.

    Pool sizing matters on managed free tiers: Supabase's smallest plans cap
    total connections (15 on the free tier), and the worker rate-limiter holds
    a second, independent asyncpg pool. Both are bounded by settings so a
    deployment can fit inside the provider's limit.
    """
    global engine, async_session_factory

    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    logger.info(
        "database.engine_initialized",
        host=settings.database_url.split("@")[-1],
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    return engine


async def schema_is_current() -> bool:
    """True if the database already has the tables the app queries.

    A missing schema is otherwise invisible: the app boots, the workers start,
    and every query fails with a relation error logged once per poll.
    """
    if engine is None:
        return False
    from sqlalchemy import text

    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text("SELECT to_regclass('public.task_queue') IS NOT NULL")
            )
            return bool(result.scalar())
    except Exception:
        logger.warning("database.schema_probe_failed", exc_info=True)
        return False


async def get_session() -> AsyncSession:  # type: ignore[misc]
    """Dependency that yields a database session."""
    if async_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    async with async_session_factory() as session:
        yield session


def session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the live session factory.

    Callers must resolve the factory through this accessor rather than importing
    the module-level name, which binds at import time and cannot be swapped.
    """
    if async_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return async_session_factory


def set_session_factory(factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Override the session factory (used by the test suite)."""
    global async_session_factory
    async_session_factory = factory


async def close_db() -> None:
    """Dispose of the database engine."""
    global engine
    if engine is not None:
        await engine.dispose()
        logger.info("database.engine_disposed")
