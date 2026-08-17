"""
Enums used by TripWeaver database models.
"""

from __future__ import annotations

import enum


class TripStatus(str, enum.Enum):
    DRAFT = "draft"
    RESEARCHING = "researching"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    BOOKED = "booked"
    CANCELLED = "cancelled"
    FAILED = "failed"


class BookingType(str, enum.Enum):
    FLIGHT = "flight"
    HOTEL = "hotel"


class BookingStatus(str, enum.Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    BOOKED = "booked"
    BOOKING_FAILED = "booking_failed"
    CANCELLED = "cancelled"


class ApprovalDecision(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"