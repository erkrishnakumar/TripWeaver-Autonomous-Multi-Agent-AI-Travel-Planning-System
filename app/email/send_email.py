"""
Real email delivery via Resend -- the ONE place this project ever sends an
email. Currently used only for password reset (app/api/main.py's
forgot-password endpoint); the module is written generically enough
(send_email() is the primitive, send_password_reset_email() a specific
use of it) that a future notification (booking confirmed, trip failed)
can reuse the same primitive without a second integration.

Resend chosen over raw SMTP: a generous free tier, and their shared
onboarding@resend.dev sender works immediately with no domain verification
-- appropriate for this project's current stage (no real end users, no
owned domain to verify against). Swapping providers later only touches
this one file, since app/api/main.py calls send_password_reset_email(),
never the Resend SDK directly.
"""

from __future__ import annotations

import logging

import resend

from app.config import settings

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """Raised when Resend's API call itself fails (bad key, network error,
    rate limit, etc.) -- callers must NOT let this surface a different
    response to the client than a successful send would (see forgot-
    password's own docstring for why: revealing "email failed to send"
    vs. "email sent" would itself leak whether an account exists)."""


def send_password_reset_email(to_email: str, reset_token: str) -> None:
    """Sends the real reset-token email. Raises EmailSendError on any
    failure -- the caller (forgot-password) is responsible for catching
    this and still returning its normal, generic response regardless, so
    a Resend outage can't be used to distinguish a real account from a
    fake one by which response shape comes back."""
    settings.validate_resend()
    resend.api_key = settings.resend_api_key

    try:
        resend.Emails.send(
            {
                "from": settings.resend_from_email,
                "to": to_email,
                "subject": "Reset your TripWeaver password",
                "text": (
                    "Someone requested a password reset for your TripWeaver account.\n\n"
                    f"Reset token: {reset_token}\n\n"
                    f"This token expires in {settings.password_reset_token_expire_minutes} "
                    "minutes. Use it with POST /auth/reset-password.\n\n"
                    "If you didn't request this, you can safely ignore this email."
                ),
            }
        )
    except Exception as e:
        raise EmailSendError(f"Resend API call failed: {e}") from e

    logger.info("Password reset email sent")
