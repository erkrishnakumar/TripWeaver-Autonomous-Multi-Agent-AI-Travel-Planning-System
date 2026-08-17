"""
Flight option model.

Represents a candidate flight discovered by the Researcher agent.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.types import uuid_pk, variant_json

if TYPE_CHECKING:
    from app.db.models.booking import Booking
    from app.db.models.trip import Trip


class FlightOption(Base, TimestampMixin):
    __tablename__ = "flight_options"

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

    total_price_usd: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    cabin_class: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="economy",
    )

    stops_outbound: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
    )

    segments: Mapped[list] = mapped_column(
        variant_json,
        nullable=False,
        default=list,
    )

    is_mock: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
    )

    is_selected: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
    )

    trip: Mapped["Trip"] = relationship(
        back_populates="flight_options",
    )

    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="flight_option",
    )

    __table_args__ = (
        Index(
            "ix_flight_options_trip_price",
            "trip_id",
            "total_price_usd",
        ),
        Index(
            "ix_flight_options_selected",
            "is_selected",
        ),
    )