"""
Rate limiting (Phase 9) -- one shared `limiter` instance, imported by
app/api/main.py both to register the global default limit/exception
handler and to apply stricter per-route limits on the auth endpoints.

Backed by Valkey (limits' redis-compatible storage), not slowapi's default
in-memory counter -- an in-memory limiter resets on every restart and
disagrees between multiple API replicas (each would enforce its own
separate count), neither of which is acceptable once this runs as more
than a single local process. Keyed by remote IP address (slowapi's
default) -- this project has no per-user API key concept for
unauthenticated requests (register/login/forgot-password are exactly the
endpoints an attacker would hit without ever having a token), so IP is the
only identity available at this layer.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.rate_limit_storage_url,
    # Applies to every route that doesn't declare its own @limiter.limit(...)
    # override -- generous enough not to bother a real client under normal
    # use, low enough to blunt a naive scripted flood against the API as a
    # whole (Gate 2's real-booking endpoints included).
    default_limits=["60/minute"],
)
