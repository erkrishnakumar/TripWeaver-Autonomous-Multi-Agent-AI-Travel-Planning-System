"""
FastAPI dependencies — currently just the DB session. Wraps
app.db.session.get_session() (already used by flow.py) rather than
duplicating engine/session-factory setup here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session() as session:
        yield session
