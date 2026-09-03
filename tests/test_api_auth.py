"""
Tests for auth: app/auth/tokens.py directly, and the API layer's
register/login/protected-endpoint/ownership behavior end to end through a
real ASGI request (httpx.AsyncClient + ASGITransport), not just calling the
endpoint functions directly -- this is the layer where FastAPI's dependency
injection (get_current_user, get_db) actually resolves, which a direct
function call would skip entirely.

Same in-memory-SQLite discipline as tests/test_confirm_booking.py: the real
app, a real (temporary) database, only Celery's .delay() calls need never
be exercised here since none of these tests touch POST /trips.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import jwt as _pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.api.main import app
from app.auth.reset_tokens import generate_reset_token, hash_reset_token
from app.auth.tokens import InvalidTokenError, create_access_token, decode_access_token
from app.config import settings
from app.db.base import Base
from app.db.models import PasswordResetToken, User
from app.tools.create_trip import create_trip


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    """create_access_token()/decode_access_token() both call
    settings.validate_jwt(), which raises if JWT_SECRET_KEY is unset -- true
    in a clean test environment. Patch a fixed value directly onto the
    settings singleton rather than requiring every dev machine's .env to
    have one just to run the test suite."""
    monkeypatch.setattr(settings, "jwt_secret_key", "test-secret-key-not-for-real-use")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def client(session):
    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register_and_login(
    client: AsyncClient, email: str, password: str = "correcthorse"
) -> str:
    resp = await client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


class TestTokens:
    def test_round_trip(self):
        user_id = str(uuid.uuid4())
        token = create_access_token(user_id)
        assert decode_access_token(token) == user_id

    def test_garbage_token_raises(self):
        with pytest.raises(InvalidTokenError):
            decode_access_token("not-a-real-token")

    def test_tampered_token_raises(self):
        token = create_access_token(str(uuid.uuid4()))
        with pytest.raises(InvalidTokenError):
            decode_access_token(token + "x")


class TestRegisterAndLogin:
    async def test_register_creates_a_user(self, client):
        resp = await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "correcthorse"}
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["email"] == "jane@example.com"
        assert body["is_active"] is True
        assert "hashed_password" not in body  # UserRead never exposes it

    async def test_duplicate_email_is_rejected(self, client):
        await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "correcthorse"}
        )
        resp = await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "differentpass"}
        )
        assert resp.status_code == 409

    async def test_password_too_short_is_rejected(self, client):
        resp = await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "short"}
        )
        assert resp.status_code == 422

    async def test_login_returns_a_bearer_token(self, client):
        await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "correcthorse"}
        )
        resp = await client.post(
            "/auth/login", json={"email": "jane@example.com", "password": "correcthorse"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert len(body["access_token"]) > 20

    async def test_login_with_wrong_password_is_rejected(self, client):
        await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "correcthorse"}
        )
        resp = await client.post(
            "/auth/login", json={"email": "jane@example.com", "password": "wrongpassword"}
        )
        assert resp.status_code == 401

    async def test_login_with_unknown_email_is_rejected(self, client):
        resp = await client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "whatever"}
        )
        assert resp.status_code == 401


class TestProtectedEndpoints:
    async def test_request_without_token_is_rejected(self, client):
        resp = await client.get(f"/trips/{uuid.uuid4()}")
        assert resp.status_code == 401

    async def test_request_with_garbage_token_is_rejected(self, client):
        resp = await client.get(
            f"/trips/{uuid.uuid4()}", headers={"Authorization": "Bearer garbage"}
        )
        assert resp.status_code == 401

    async def test_authenticated_user_can_read_own_trip(self, client, session):
        token = await _register_and_login(client, "jane@example.com")
        user_id = uuid.UUID(_pyjwt.decode(token, options={"verify_signature": False})["sub"])
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date(2026, 10, 15),
            user_id=user_id,
        )
        await session.commit()

        resp = await client.get(f"/trips/{trip.id}", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["id"] == str(trip.id)

    async def test_user_cannot_read_another_users_trip(self, client, session):
        owner_token = await _register_and_login(client, "owner@example.com")
        owner_id = uuid.UUID(_pyjwt.decode(owner_token, options={"verify_signature": False})["sub"])
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date(2026, 10, 15),
            user_id=owner_id,
        )
        await session.commit()

        other_token = await _register_and_login(client, "someone-else@example.com")
        resp = await client.get(
            f"/trips/{trip.id}", headers={"Authorization": f"Bearer {other_token}"}
        )
        assert resp.status_code == 404  # not 403 -- existence isn't confirmed either

    async def test_trip_with_no_owner_is_not_readable_by_anyone(self, client, session):
        """A Trip created with user_id=None (e.g. by the CLI/Celery path,
        which has no authenticated user) must not become readable by
        whichever authenticated user happens to ask -- _trip_or_404()
        compares trip.user_id to current_user.id, and None != any real
        UUID, so this is already covered structurally; this test pins that
        behavior explicitly rather than leaving it implicit."""
        trip = await create_trip(
            session,
            origin_iata="JFK",
            destination_iata="ATL",
            depart_date=date(2026, 10, 15),
        )
        await session.commit()
        assert trip.user_id is None

        token = await _register_and_login(client, "jane@example.com")
        resp = await client.get(f"/trips/{trip.id}", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404


class TestForgotAndResetPassword:
    async def test_full_reset_flow_changes_the_password(self, client):
        await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "originalpass"}
        )

        forgot_resp = await client.post("/auth/forgot-password", json={"email": "jane@example.com"})
        assert forgot_resp.status_code == 200
        reset_token = forgot_resp.json()["reset_token"]
        assert reset_token is not None

        reset_resp = await client.post(
            "/auth/reset-password",
            json={"token": reset_token, "new_password": "brandnewpass"},
        )
        assert reset_resp.status_code == 200

        # Old password no longer works.
        old_login = await client.post(
            "/auth/login", json={"email": "jane@example.com", "password": "originalpass"}
        )
        assert old_login.status_code == 401

        # New password works.
        new_login = await client.post(
            "/auth/login", json={"email": "jane@example.com", "password": "brandnewpass"}
        )
        assert new_login.status_code == 200

    async def test_unknown_email_does_not_error(self, client):
        """No user-enumeration protection in this dev-mode delivery
        mechanism (see ForgotPasswordResponse's docstring) -- but the
        endpoint itself must still respond cleanly, not 500, for an email
        that doesn't match any account."""
        resp = await client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
        assert resp.status_code == 200
        assert resp.json()["reset_token"] is None

    async def test_reset_token_is_single_use(self, client):
        await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "originalpass"}
        )
        forgot_resp = await client.post("/auth/forgot-password", json={"email": "jane@example.com"})
        reset_token = forgot_resp.json()["reset_token"]

        first = await client.post(
            "/auth/reset-password", json={"token": reset_token, "new_password": "firstchange"}
        )
        assert first.status_code == 200

        second = await client.post(
            "/auth/reset-password", json={"token": reset_token, "new_password": "secondchange"}
        )
        assert second.status_code == 400

    async def test_garbage_token_is_rejected(self, client):
        resp = await client.post(
            "/auth/reset-password", json={"token": "not-a-real-token", "new_password": "whatever1"}
        )
        assert resp.status_code == 400

    async def test_expired_token_is_rejected(self, client, session):
        resp = await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "originalpass"}
        )
        user_id = uuid.UUID(resp.json()["id"])
        assert (await session.get(User, user_id)) is not None

        raw_token = generate_reset_token()
        session.add(
            PasswordResetToken(
                id=uuid.uuid4(),
                user_id=user_id,
                token_hash=hash_reset_token(raw_token),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),  # already expired
            )
        )
        await session.commit()

        resp = await client.post(
            "/auth/reset-password", json={"token": raw_token, "new_password": "wontwork1"}
        )
        assert resp.status_code == 400

    async def test_new_password_too_short_is_rejected(self, client):
        forgot_resp_setup = await client.post(
            "/auth/register", json={"email": "jane@example.com", "password": "originalpass"}
        )
        assert forgot_resp_setup.status_code == 201
        forgot_resp = await client.post("/auth/forgot-password", json={"email": "jane@example.com"})
        reset_token = forgot_resp.json()["reset_token"]

        resp = await client.post(
            "/auth/reset-password", json={"token": reset_token, "new_password": "short"}
        )
        assert resp.status_code == 422
