"""
Reusable SQLAlchemy types and helpers for TripWeaver models.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column


# Use JSONB on PostgreSQL and JSON on other databases such as SQLite.
variant_json = JSON().with_variant(JSONB, "postgresql")


def uuid_pk() -> Mapped[uuid.UUID]:
    """
    Reusable UUID primary-key definition.

    PostgreSQL:
        Uses native UUID.

    SQLite:
        Falls back to String(36) for local/dev testing.
    """
    return mapped_column(
        UUID(as_uuid=True).with_variant(String(36), "sqlite"),
        primary_key=True,
        default=uuid.uuid4,
    )