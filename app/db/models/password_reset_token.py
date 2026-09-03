"""
Password reset token model.

A single-use, expiring, hashed token issued by POST /auth/forgot-password
and consumed by POST /auth/reset-password. Never stores the raw token --
only a SHA-256 hash of it (see app/auth/reset_tokens.py) -- same "never
store the sensitive value in plaintext" principle as User.hashed_password,
though a different hash function on purpose: bcrypt is for low-entropy
human passwords that need brute-force resistance; a 32-byte
cryptographically random token has no such weakness, so a fast hash used
purely as a lookup key is the right tool here, not a slow one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.types import uuid_pk

if TYPE_CHECKING:
    from app.db.models.user import User


class PasswordResetToken(Base, TimestampMixin):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(
        String(64),  # a SHA-256 hex digest is always exactly 64 characters
        nullable=False,
        unique=True,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # Set the moment the token is successfully consumed by
    # POST /auth/reset-password -- makes it single-use: a second attempt
    # with the same raw token finds a non-None used_at and is rejected,
    # even if it's still within its expiry window.
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user: Mapped[User] = relationship()

    __table_args__ = (Index("ix_password_reset_tokens_user_id", "user_id"),)
