"""
Declarative base for all TripWeaver models.

The naming_convention here matters more than it looks: without it, SQLAlchemy
lets the database auto-generate constraint names (e.g. Postgres picks its own
name for a foreign key). Alembic's autogenerate then can't reliably tell
"this constraint changed" from "this is a brand new constraint" across
migrations, because the names aren't deterministic. Setting this up front
avoids messy/duplicate migrations later.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Shared created_at/updated_at columns for every table that wants them.

    DateTime(timezone=True) matters here, not just style: datetime.now(UTC)
    produces a timezone-AWARE value, but a bare DateTime column maps to
    Postgres' TIMESTAMP WITHOUT TIME ZONE. asyncpg strictly rejects binding
    an aware datetime to a naive column (DataError: can't subtract offset-
    naive and offset-aware datetimes) -- a real bug that stayed hidden
    through every prior test because they all ran against permissive
    in-memory SQLite, only surfacing on the first real write through the
    full agent Flow against actual Postgres.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
