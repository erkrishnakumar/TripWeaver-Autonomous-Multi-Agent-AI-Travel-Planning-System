"""
JWT access-token issuance/verification — the ONLY place a token is ever
encoded or decoded anywhere in this codebase, mirroring passwords.py's
"one place touches the sensitive thing" discipline.

Stateless HS256 bearer tokens, not server-side sessions: this project has
no existing session store need beyond auth (Valkey is used for Celery, not
sessions), and a stateless token means app/api/deps.py's get_current_user()
never needs a database round-trip just to check a session is still valid --
only to load the User row itself. No refresh-token flow yet; a token is
simply valid until JWT_ACCESS_TOKEN_EXPIRE_MINUTES elapses, then the client
must log in again. Revocation-before-expiry (e.g. on password change) is a
known, accepted gap for this first version -- see docs/Auth_Requirement.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.config import settings

_SUBJECT_CLAIM = "sub"
_EXPIRY_CLAIM = "exp"


class InvalidTokenError(Exception):
    """Raised for any decode failure -- expired, malformed, or wrong
    signature. Deliberately one exception type, not three: the API layer
    treats all of these identically (401, "could not validate credentials")
    -- distinguishing them to the client would leak whether a token merely
    expired vs. was forged, with no legitimate use for that distinction."""


def create_access_token(user_id: str) -> str:
    """user_id is the User.id (UUID) as a string -- stored in the standard
    'sub' (subject) claim per JWT convention, not a custom claim name."""
    settings.validate_jwt()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload: dict[str, Any] = {_SUBJECT_CLAIM: user_id, _EXPIRY_CLAIM: expires_at}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str:
    """Returns the user_id encoded in the token, or raises InvalidTokenError.
    PyJWT itself validates the 'exp' claim during decode() -- an expired
    token raises ExpiredSignatureError, caught below along with every other
    decode failure and re-raised as this module's own InvalidTokenError."""
    settings.validate_jwt()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as e:
        raise InvalidTokenError("Token is invalid or expired.") from e

    user_id = payload.get(_SUBJECT_CLAIM)
    if not isinstance(user_id, str):
        raise InvalidTokenError("Token payload is missing a valid subject claim.")
    return user_id
