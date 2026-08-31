"""
Celery tasks. Starts with a trivial ping task to verify the whole pipeline
(enqueue -> Valkey -> worker picks it up -> executes -> result stored)
works end to end before wiring in anything that actually matters (running
the CrewAI Flow) -- see docs/TripWeaver_Roadmap.md's Phase 8 notes.
"""

from __future__ import annotations

from app.worker.celery_app import celery_app


@celery_app.task(name="ping")
def ping() -> str:
    return "pong"
