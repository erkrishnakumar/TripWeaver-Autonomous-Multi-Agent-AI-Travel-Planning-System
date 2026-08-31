"""
User model — the identity every Trip will eventually belong to.

Built now, ahead of Phase 8's API layer, because Duffel's Service
Agreement requires this app to serve a closed, authenticated user group
(see docs/Auth_Requirement.md) — Phase 8 cannot start serving real
requests without a real identity for a request to be authenticated
against existing first.

hashed_password is populated by app/auth/passwords.py's hash_password()
ONLY — nothing else in this codebase should ever read or write a
password's plaintext or hash directly.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.types import uuid_pk

if TYPE_CHECKING:
    from app.db.models.trip import Trip


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()

    email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        unique=True,
        index=True,
    )

    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # A deactivated user must never authenticate again, without deleting
    # their row (and every Trip it's linked to) outright — soft-disable,
    # not hard-delete, matching this project's "never silently lose data"
    # posture elsewhere (e.g. Booking rows are never deleted, only
    # status-transitioned).
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    trips: Mapped[list[Trip]] = relationship(back_populates="user")
