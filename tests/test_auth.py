"""
Tests for the auth foundation: app/auth/passwords.py and
app/db/models/user.py — built ahead of Phase 8's API layer per
docs/Auth_Requirement.md (Duffel's Service Agreement requires this app to
serve a closed, authenticated user group before it's reachable by anyone
but a developer running it locally).

Password tests are pure logic, no DB involved. User model tests use a real
in-memory async SQLite database — same "mock the network boundary, run
persistence for real" convention as tests/test_propose_booking.py — since
the whole point of a uniqueness constraint or a foreign key is only
genuinely verified by a real database enforcing it, not by mocking the
session.
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.passwords import PasswordTooLongError, hash_password, verify_password
from app.db.base import Base
from app.db.models import Trip, User

# NOTE: no module-level `pytestmark = pytest.mark.asyncio` -- pyproject.toml's
# asyncio_mode = "auto" already detects async tests without it, and this
# file mixes sync (pure password-hashing logic) and async (real-DB) tests;
# a blanket mark on the sync ones raises a pytest warning.


# ---------------------------------------------------------------------------
# app/auth/passwords.py
# ---------------------------------------------------------------------------


class TestHashPassword:
    def test_hash_is_not_the_plaintext(self):
        hashed = hash_password("correct horse battery staple")
        assert hashed != "correct horse battery staple"

    def test_two_hashes_of_the_same_password_differ(self):
        """bcrypt salts per-call -- this is correct behavior, not a bug."""
        first = hash_password("same-password")
        second = hash_password("same-password")
        assert first != second

    def test_password_over_72_bytes_is_rejected_not_silently_truncated(self):
        # bcrypt's limit is 72 BYTES, not characters -- use a multi-byte
        # character so a naive char-count check would miss this.
        too_long = "é" * 40  # 2 bytes/char in UTF-8 -> 80 bytes, over the limit
        with pytest.raises(PasswordTooLongError):
            hash_password(too_long)

    def test_password_at_exactly_72_bytes_is_accepted(self):
        exactly_72 = "a" * 72
        hash_password(exactly_72)  # must not raise


class TestVerifyPassword:
    def test_correct_password_verifies(self):
        hashed = hash_password("correct-password")
        assert verify_password("correct-password", hashed) is True

    def test_wrong_password_does_not_verify(self):
        hashed = hash_password("correct-password")
        assert verify_password("wrong-password", hashed) is False

    def test_verification_is_case_sensitive(self):
        hashed = hash_password("CaseSensitive123")
        assert verify_password("casesensitive123", hashed) is False


# ---------------------------------------------------------------------------
# app/db/models/user.py
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class TestUserModel:
    async def test_user_can_be_created_and_read_back(self, session_factory):
        async with session_factory() as session:
            user = User(email="jane@example.com", hashed_password=hash_password("s3cret!"))
            session.add(user)
            await session.commit()

        async with session_factory() as session:
            result = await session.execute(select(User).where(User.email == "jane@example.com"))
            fetched = result.scalar_one()
            assert fetched.email == "jane@example.com"
            assert fetched.is_active is True
            assert verify_password("s3cret!", fetched.hashed_password) is True

    async def test_duplicate_email_is_rejected(self, session_factory):
        """The unique index on User.email must actually be enforced by the
        database, not just declared -- this is exactly the kind of thing
        that silently stops mattering if someone later drops the index in
        a migration without a test catching it."""
        async with session_factory() as session:
            session.add(User(email="dup@example.com", hashed_password=hash_password("a")))
            await session.commit()

        async with session_factory() as session:
            session.add(User(email="dup@example.com", hashed_password=hash_password("b")))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_trip_can_be_linked_to_a_user(self, session_factory):
        async with session_factory() as session:
            user = User(email="traveler@example.com", hashed_password=hash_password("pw"))
            session.add(user)
            await session.flush()

            trip = Trip(
                user_id=user.id,
                origin_iata="JFK",
                destination_iata="ATL",
                depart_date=date(2026, 9, 27),
                adults=1,
            )
            session.add(trip)
            await session.commit()

        async with session_factory() as session:
            result = await session.execute(select(Trip).where(Trip.origin_iata == "JFK"))
            fetched_trip = result.scalar_one()
            assert fetched_trip.user_id == user.id

    async def test_trip_without_a_user_is_still_allowed(self, session_factory):
        """user_id is nullable by design -- Phase 8 doesn't exist yet, so
        nothing authenticates real requests today. This must keep working
        exactly like it did before User existed at all."""
        async with session_factory() as session:
            trip = Trip(
                origin_iata="JFK",
                destination_iata="ATL",
                depart_date=date(2026, 9, 27),
                adults=1,
                requester_email="anonymous@example.com",
            )
            session.add(trip)
            await session.commit()  # must not raise

        async with session_factory() as session:
            result = await session.execute(
                select(Trip).where(Trip.requester_email == "anonymous@example.com")
            )
            fetched_trip = result.scalar_one()
            assert fetched_trip.user_id is None
