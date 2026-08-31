"""
Password hashing — the ONLY place a password's plaintext or hash is ever
touched anywhere in this codebase.

Uses bcrypt directly, not passlib: passlib's bcrypt backend has had
repeated compatibility breaks against newer bcrypt releases, and this
project only ever needs the two operations below (hash, verify), not
passlib's broader multi-algorithm abstraction — a narrower dependency
surface for something this security-sensitive is a feature, not a
limitation.

This module exists ahead of any login endpoint (Phase 8 doesn't exist yet)
because it's needed to define app/db/models/user.py's hashed_password
column meaningfully — see docs/Auth_Requirement.md for why authentication
is being built now rather than deferred to "whenever Phase 8 starts."
"""

from __future__ import annotations

import bcrypt

# bcrypt has a hard 72-BYTE input limit (not 72 characters — a difference
# that matters for multi-byte UTF-8 passwords) and silently truncates
# anything longer instead of raising, per the underlying C library's own
# contract. Rejecting an over-length password explicitly here means a user
# never ends up with a hash of a truncated password without knowing it —
# which would otherwise look successful right up until they can't log back
# in with the password they actually typed.
_MAX_PASSWORD_BYTES = 72


class PasswordTooLongError(ValueError):
    """Raised instead of silently truncating -- see module docstring."""


def hash_password(plain_password: str) -> str:
    """One-way conversion from a plaintext password to a stored hash.
    Never persist plain_password anywhere else, ever, even in logs.

    bcrypt.gensalt() generates a fresh random salt on every call, so
    hashing the same password twice produces two different hashes — this
    is bcrypt working correctly, not a bug; verify_password() (below)
    still correctly matches either hash against the original password."""
    encoded = plain_password.encode("utf-8")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"Password is {len(encoded)} bytes, but bcrypt only supports up to "
            f"{_MAX_PASSWORD_BYTES} bytes — reject this at the API boundary "
            "with a clear error, don't silently truncate it."
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Checks a plaintext password against a stored hash.

    Uses bcrypt.checkpw() specifically (not re-hashing and comparing with
    ==) since checkpw() is a constant-time comparison — comparing strings
    with == leaks timing information proportional to how many leading
    characters match, a real (if narrow) side-channel for password
    verification specifically."""
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
