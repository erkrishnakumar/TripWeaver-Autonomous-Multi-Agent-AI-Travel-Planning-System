"""
Enums used by TripWeaver database models.
"""

from __future__ import annotations

import enum


class TripStatus(enum.StrEnum):
    DRAFT = "draft"
    RESEARCHING = "researching"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    BOOKED = "booked"
    CANCELLED = "cancelled"
    FAILED = "failed"


class BookingType(enum.StrEnum):
    FLIGHT = "flight"
    HOTEL = "hotel"


class BookingStatus(enum.StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    BOOKED = "booked"
    BOOKING_FAILED = "booking_failed"
    CANCELLED = "cancelled"


class ApprovalDecision(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
