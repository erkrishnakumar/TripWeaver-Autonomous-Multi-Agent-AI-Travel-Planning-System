"""
Audit log model.

Append-only record of meaningful state transitions in TripWeaver.

Audit records intentionally do not use TimestampMixin because they should
never be updated after creation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, UTC

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import uuid_pk, variant_json


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = uuid_pk()

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
        doc=(
            "Examples: trip.created, booking.approved, "
            "booking.booked"
        ),
    )

    payload: Mapped[dict] = mapped_column(
        variant_json,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now(UTC),
    )

    __table_args__ = (
        Index(
            "ix_audit_log_trip_id",
            "trip_id",
        ),
        Index(
            "ix_audit_log_booking_id",
            "booking_id",
        ),
        Index(
            "ix_audit_log_event_type",
            "event_type",
        ),
    )