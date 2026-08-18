"""
Reusable SQLAlchemy types and helpers for TripWeaver models.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

variant_json = JSON().with_variant(JSONB, "postgresql")

# sqlalchemy.Uuid (not dialects.postgresql.UUID) correctly handles both:
# native UUID storage on Postgres, and a string-based fallback on SQLite/
# other dialects — with the Python-side UUID<->str conversion built in for
# both. The Postgres-specific UUID type only has that conversion on
# Postgres; on SQLite it silently degrades to a bare String column with no
# conversion, which breaks the moment a real uuid.UUID object is inserted.
def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )