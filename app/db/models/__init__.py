"""
TripWeaver SQLAlchemy models.

Importing all models here ensures that every model is registered
with Base.metadata before Alembic performs autogeneration.
"""

from app.db.models.approval import Approval
from app.db.models.audit_log import AuditLog
from app.db.models.booking import Booking
from app.db.models.car_rental_option import CarRentalOption
from app.db.models.flight_option import FlightOption
from app.db.models.hotel_option import HotelOption
from app.db.models.itinerary import Itinerary
from app.db.models.trip import Trip
from app.db.models.user import User

__all__ = [
    "Approval",
    "AuditLog",
    "Booking",
    "CarRentalOption",
    "FlightOption",
    "HotelOption",
    "Itinerary",
    "Trip",
    "User",
]
