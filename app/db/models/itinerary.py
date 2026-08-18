from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Index, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.types import uuid_pk, variant_json

if TYPE_CHECKING:
    from app.db.models.trip import Trip


class Itinerary(Base, TimestampMixin):
    __tablename__ = "itineraries"

    id: Mapped[uuid.UUID] = uuid_pk()

    trip_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
    )
    day_number: Mapped[int] = mapped_column(nullable=False)
    day_date: Mapped[date] = mapped_column(Date, nullable=False)
    plan: Mapped[dict] = mapped_column(variant_json, nullable=False, default=dict)

    trip: Mapped["Trip"] = relationship(back_populates="itineraries")

    __table_args__ = (Index("ix_itineraries_trip_day", "trip_id", "day_number", unique=True),)