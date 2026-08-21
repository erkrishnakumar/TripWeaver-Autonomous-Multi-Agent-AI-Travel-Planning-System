"""
Hotel option model.

Represents a candidate hotel discovered by the Researcher agent.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.types import uuid_pk

if TYPE_CHECKING:
    from app.db.models.booking import Booking
    from app.db.models.trip import Trip


class HotelOption(Base, TimestampMixin):
    __tablename__ = "hotel_options"

    id: Mapped[uuid.UUID] = uuid_pk()

    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "trips.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="duffel",
    )

    provider_offer_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    total_price_usd: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    check_in: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    check_out: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    star_rating: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    is_mock: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
    )

    is_selected: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
    )

    trip: Mapped[Trip] = relationship(
        back_populates="hotel_options",
    )

    bookings: Mapped[list[Booking]] = relationship(
        back_populates="hotel_option",
    )

    __table_args__ = (
        Index(
            "ix_hotel_options_trip_price",
            "trip_id",
            "total_price_usd",
        ),
        Index(
            "ix_hotel_options_selected",
            "is_selected",
        ),
    )
