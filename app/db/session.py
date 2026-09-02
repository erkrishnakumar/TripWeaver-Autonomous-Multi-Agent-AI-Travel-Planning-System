"""
Database engine and session factory.

Uses an async engine (asyncpg driver) since the API layer (Phase 8) will be
FastAPI, which is async-first. Alembic migrations, by contrast, run
synchronously — that's normal and handled separately in alembic/env.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

# DATABASE_URL is a plain postgresql:// URL; the async engine needs the
# asyncpg driver explicitly in the scheme.
_ASYNC_DATABASE_URL = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(_ASYNC_DATABASE_URL, echo=False, poolclass=NullPool)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Usage:
    async with get_session() as session:
        session.add(some_row)
        await session.commit()
    """
    async with async_session_factory() as session:
        yield session
