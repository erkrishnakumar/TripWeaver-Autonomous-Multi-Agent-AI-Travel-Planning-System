"""
Trip model.

Represents one trip-planning request initiated by a user.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.enums import TripStatus
from app.db.models.types import uuid_pk

if TYPE_CHECKING:
    from app.db.models.booking import Booking
    from app.db.models.flight_option import FlightOption
    from app.db.models.hotel_option import HotelOption
    from app.db.models.itinerary import Itinerary


class Trip(Base, TimestampMixin):
    __tablename__ = "trips"

    id: Mapped[uuid.UUID] = uuid_pk()

    # No auth system yet — track the requester loosely by email.
    requester_email: Mapped[str | None] = mapped_column(
        String(320),
        nullable=True,
    )

    origin_iata: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    destination_iata: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    depart_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    return_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )

    adults: Mapped[int] = mapped_column(
        nullable=False,
        default=1,
    )

    max_budget_usd: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    status: Mapped[TripStatus] = mapped_column(
        Enum(
            TripStatus,
            name="trip_status",
            values_callable=lambda enum_cls: [
                item.value for item in enum_cls
            ],
        ),
        default=TripStatus.DRAFT,
        nullable=False,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    itineraries: Mapped[list["Itinerary"]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
    )

    flight_options: Mapped[list["FlightOption"]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
    )

    hotel_options: Mapped[list["HotelOption"]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
    )

    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index(
            "ix_trips_requester_email",
            "requester_email",
        ),
        Index(
            "ix_trips_status",
            "status",
        ),
        Index(
            "ix_trips_origin_destination",
            "origin_iata",
            "destination_iata",
        ),
    )