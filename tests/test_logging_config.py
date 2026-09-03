"""
Tests for app/logging_config.py -- the shared logging setup used by the
API process, the Celery worker, and the CLI entrypoint.

The one thing that actually matters here: a log call WITHOUT
extra={"trip_id": ...} must not crash. %(trip_id)s appears in every log
line's format string (see _LOG_FORMAT), and logging.Formatter raises a
hard KeyError the first time it hits a record missing that attribute --
_DefaultTripIdFilter exists specifically to prevent that, so this is a
real regression risk if it were ever removed, not a hypothetical.
"""

from __future__ import annotations

import io
import logging

from app.logging_config import configure_logging


def _capture_one_log(log_call) -> str:
    """Runs configure_logging(), swaps the resulting handler's stream for
    an in-memory buffer, executes log_call, and returns exactly what was
    written -- isolated from pytest's own log capture machinery, which
    would otherwise mask a real formatting crash (a Formatter exception is
    caught and substituted with its own placeholder text by logging's
    internal error handling, not re-raised, so asserting against captured
    text is the only way to actually detect a KeyError here)."""
    configure_logging()
    root = logging.getLogger()
    handler = root.handlers[0]
    buffer = io.StringIO()
    handler.stream = buffer
    log_call()
    handler.flush()
    return buffer.getvalue()


class TestConfigureLogging:
    def test_log_call_with_trip_id_includes_it(self):
        logger = logging.getLogger("test.with_trip_id")
        output = _capture_one_log(lambda: logger.info("hello", extra={"trip_id": "abc-123"}))
        assert "trip=abc-123" in output
        assert "hello" in output

    def test_log_call_without_trip_id_does_not_crash(self):
        """The real regression case: no extra={"trip_id": ...} at all --
        e.g. an app-startup message with no trip in scope yet."""
        logger = logging.getLogger("test.without_trip_id")
        output = _capture_one_log(lambda: logger.info("startup message"))
        assert "trip=-" in output
        assert "startup message" in output
        assert "KeyError" not in output

    def test_calling_configure_logging_twice_does_not_duplicate_handlers(self):
        configure_logging()
        configure_logging()
        assert len(logging.getLogger().handlers) == 1
