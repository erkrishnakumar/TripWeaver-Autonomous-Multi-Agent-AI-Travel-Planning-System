"""
Centralized logging configuration — the ONE place logging.basicConfig()-
equivalent setup happens, so format/level can't drift between the three
separate Python processes this project runs (the FastAPI app, the Celery
worker, and the CLI `python -m app.agents.flow` entrypoint).

Deliberately stdlib `logging`, not a third-party structured-logging
library (structlog, etc.) -- this project only needs consistent, greppable
plain-text lines with a trip_id tag, not JSON output or log shipping. Same
narrow-dependency reasoning as choosing bcrypt over passlib and PyJWT over
python-jose elsewhere in this codebase.

Every log call in this project that concerns a specific trip should pass
`extra={"trip_id": trip_id}` so it's tagged and greppable
(`grep 'trip=<uuid>'` finds every log line about one run, across research,
planning, proposing, and confirming, with no need to correlate timestamps
across CrewAI's own separate console output). A trip_id-less log call
(app startup, a config error) still logs fine -- _DefaultTripIdFilter
below fills in "-" so the format string never KeyErrors on a missing field.
"""

from __future__ import annotations

import logging

from app.config import settings

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [trip=%(trip_id)s] %(message)s"


class _DefaultTripIdFilter(logging.Filter):
    """Lets %(trip_id)s appear in every log line's format string even when
    a given call site didn't pass extra={"trip_id": ...} -- without this,
    logging.Formatter raises a hard KeyError the first time a trip_id-less
    call (e.g. a startup message) hits a formatter expecting that field."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trip_id"):
            record.trip_id = "-"
        return True


def configure_logging() -> None:
    """Idempotent -- safe to call from every process entrypoint (FastAPI's
    module load, Celery's setup_logging signal, the CLI's __main__) even
    though only one of those runs in a given process; replaces the root
    logger's handlers each time rather than appending, so calling it twice
    in the same process (e.g. a test importing multiple entrypoints) never
    produces duplicated log lines."""
    handler = logging.StreamHandler()
    handler.addFilter(_DefaultTripIdFilter())
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers = [handler]
