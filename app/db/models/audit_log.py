"""
Audit log model.

Append-only record of meaningful state transitions in TripWeaver.

Audit records intentionally do not use TimestampMixin because they should
never be updated after creation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.schema import UniqueConstraint

from app.db.base import Base
from app.db.models.types import uuid_pk, variant_json


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = uuid_pk()

    # A monotonically increasing per-trip tiebreaker for ordering, computed
    # in app/tools/audit.py's log_stage_event() as a plain Python
    # MAX(sequence)+1 query -- see that function's own docstring for the
    # full story. NEITHER created_at (wall-clock) NOR time.monotonic_ns()
    # turned out reliable for this: both returned IDENTICAL values for two
    # back-to-back writes, verified live (Windows clock resolution is
    # coarser than either name implies). Deliberately NOT a DB identity/
    # sequence column either (tried that, hit a real SQLite-vs-Postgres DDL
    # incompatibility -- SQLite has no equivalent for a non-PK column).
    # A per-trip MAX+1 is only safe because one trip's audit events are
    # always written sequentially by whichever single worker is currently
    # processing that trip, never concurrently by two workers for the same
    # trip_id -- see log_stage_event()'s docstring for what would need to
    # change if that invariant is ever violated.
    sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "trips.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "bookings.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        doc=("Examples: trip.created, booking.approved, booking.booked"),
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        variant_json,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_audit_log_trip_id", "trip_id"),
        Index("ix_audit_log_booking_id", "booking_id"),
        Index("ix_audit_log_event_type", "event_type"),
        UniqueConstraint("trip_id", "sequence", name="uq_audit_log_trip_id_sequence"),
    )
