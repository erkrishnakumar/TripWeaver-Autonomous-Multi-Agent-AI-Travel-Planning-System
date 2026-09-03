"""
FastAPI dependencies: the DB session, and (Phase 8 auth) the current
authenticated user. get_db() wraps app.db.session.get_session() (already
used by flow.py) rather than duplicating engine/session-factory setup here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import InvalidTokenError, decode_access_token
from app.db.models import User
from app.db.session import get_session


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session() as session:
        yield session


# tokenUrl points at the login endpoint purely for OpenAPI's interactive
# docs (the "Authorize" button at /docs) -- this dependency itself only
# ever reads the Authorization header, it never calls that URL itself.
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


async def get_current_user(
    token: Annotated[str, Depends(_oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """The auth gate for every non-public endpoint. Raises 401 for a
    missing/invalid/expired token, an unknown user id, or a deactivated
    (is_active=False) user -- deliberately the same generic 401 for all of
    these (see InvalidTokenError's own docstring for why not distinguishing
    them further is intentional, not laziness)."""
    unauthorized = HTTPException(
        status_code=401,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        user_id = decode_access_token(token)
    except InvalidTokenError as e:
        raise unauthorized from e

    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError as e:
        raise unauthorized from e

    user = await db.get(User, user_uuid)
    if user is None or not user.is_active:
        raise unauthorized
    return user
