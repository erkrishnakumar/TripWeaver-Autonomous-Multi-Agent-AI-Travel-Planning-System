"""
Shared pytest fixtures, autoloaded for every test file in this directory
(no explicit import needed -- this is pytest's own convention for
tests/conftest.py).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_rate_limiting():
    """app/api/rate_limit.py's `limiter` enforces REAL limits against a
    REAL Valkey backend -- state that persists across test runs, not
    reset per test. Without this, any test file that calls the same
    endpoint more than a few times (e.g. TestForgotAndResetPassword's own
    suite, which calls /auth/register and /auth/forgot-password
    repeatedly) starts genuinely tripping the limiter and failing with a
    real 429 -- caught live once rate limiting was added. Same isolation
    principle as tests/test_api_auth.py's own JWT_SECRET_KEY/
    RESEND_API_KEY fixtures: a test's outcome must never depend on how
    many other tests happened to hit the same endpoint before it, in this
    run or any prior one."""
    from app.api.rate_limit import limiter

    limiter.enabled = False
    yield
    limiter.enabled = True
