"""
Tests for GET /car-rentals/component-client-key -- step one of the car
rental payment-tokenization flow (see docs/Car_Rental_Payment_Gap.md).
Mocks app.tools.car_rentals.create_component_client_key at its
app.api.main import site, the same Duffel-network boundary every other
tool call is mocked at in this project's API-layer tests.

Same real-ASGI-request discipline as tests/test_api_auth.py.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.api.main import app
from app.config import settings
from app.db.base import Base
from app.tools.schemas import ToolError


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
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


async def _register_and_login(client: AsyncClient, email: str) -> str:
    password = "correcthorse"
    resp = await client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


class TestComponentClientKey:
    async def test_requires_auth(self, client):
        resp = await client.get("/car-rentals/component-client-key")
        assert resp.status_code == 401

    async def test_returns_the_key_from_duffel(self, client, monkeypatch):
        import app.api.main as main_module

        monkeypatch.setattr(
            main_module, "create_component_client_key", lambda: "fake_component_client_key_value"
        )
        token = await _register_and_login(client, "jane@example.com")

        resp = await client.get(
            "/car-rentals/component-client-key", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"component_client_key": "fake_component_client_key_value"}

    async def test_duffel_error_returns_502(self, client, monkeypatch):
        import app.api.main as main_module

        monkeypatch.setattr(
            main_module,
            "create_component_client_key",
            lambda: ToolError(
                tool_name="create_component_client_key",
                error_type="duffel_api_error",
                message="Duffel Cars API returned 500: internal error",
                retryable=True,
            ),
        )
        token = await _register_and_login(client, "jane@example.com")

        resp = await client.get(
            "/car-rentals/component-client-key", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 502
