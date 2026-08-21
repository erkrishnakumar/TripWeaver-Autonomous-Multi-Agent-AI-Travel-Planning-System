"""
Approval model.

Represents the human-in-the-loop decision associated with a booking.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import ApprovalDecision
from app.db.models.types import uuid_pk

if TYPE_CHECKING:
    from app.db.models.booking import Booking


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = uuid_pk()

    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "bookings.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(
            ApprovalDecision,
            name="approval_decision",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=ApprovalDecision.PENDING,
        nullable=False,
    )

    decided_by: Mapped[str | None] = mapped_column(
        String(320),
        nullable=True,
        doc="Email/identifier of the human who approved or rejected.",
    )

    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    decision_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    booking: Mapped[Booking] = relationship(
        back_populates="approval",
    )

    __table_args__ = (
        UniqueConstraint(
            "booking_id",
            name="uq_approvals_booking_id",
        ),
        Index(
            "ix_approvals_decision",
            "decision",
        ),
    )
