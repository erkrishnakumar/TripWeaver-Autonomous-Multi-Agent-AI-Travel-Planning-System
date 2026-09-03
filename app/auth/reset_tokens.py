"""
Password reset token generation/hashing -- the ONE place a reset token is
ever created or hashed, mirroring passwords.py's/tokens.py's "one place
touches the sensitive thing" discipline.

Deliberately NOT reusing hash_password()/verify_password() from
passwords.py: those use bcrypt, which is deliberately slow (brute-force
resistance for a low-entropy human-chosen password). A reset token is a
32-byte cryptographically random value with far more entropy than any
password -- brute-forcing it is infeasible regardless of hash speed -- so
a fast hash (SHA-256) used purely as a lookup key is the right tool, and
avoids bcrypt's 72-byte input limit entirely (irrelevant here, but a real
constraint that would need working around for no benefit if reused).
"""

from __future__ import annotations

import hashlib
import secrets

_TOKEN_BYTES = 32


def generate_reset_token() -> str:
    """The raw, one-time-visible token -- returned to the caller (today: in
    the API response body directly, since no email-sending integration
    exists yet, see docs/Auth_Requirement.md; a real deployment MUST
    replace that delivery mechanism with actually emailing this value
    before going live, not return it in an HTTP response). Never stored
    anywhere -- only hash_reset_token()'s output is persisted."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_reset_token(raw_token: str) -> str:
    """Deterministic (unlike bcrypt's salted hash_password()) -- on purpose,
    since PasswordResetToken.token_hash needs to be looked up directly by
    value (SELECT ... WHERE token_hash = ?), not compared one-by-one the
    way a login flow checks a single known user's password hash."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
