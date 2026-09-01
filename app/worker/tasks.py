"""
Celery tasks. ping() is a trivial pipeline-verification task (see its own
docstring). run_trip_planning() is the real one -- wraps TripPlanningFlow's
resumable research->plan chain (app/agents/flow.py) so it can run as a
background job instead of blocking an HTTP request for however long a
multi-provider LLM pipeline takes (see docs/TripWeaver_Roadmap.md's Phase 8
notes for why this can't just be a synchronous endpoint).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from celery import Task

from app.agents.flow import TripPlanningFlow
from app.tools.audit import log_stage_event
from app.worker.celery_app import celery_app


@celery_app.task(name="ping")
def ping() -> str:
    return "pong"


async def _mark_trip_failed(trip_id: str, error: str) -> None:
    """Runs once retries are fully exhausted -- see _TripPlanningTask.
    on_failure() below. Celery's own result backend already records the
    failure, but nothing else would surface it anywhere a human/GET
    /trips/{id} would look, so this writes it to the one place that
    already does: Trip.status + AuditLog."""
    from app.db.models import Trip
    from app.db.models.enums import TripStatus
    from app.db.session import get_session

    async with get_session() as session:
        trip_row = await session.get(Trip, uuid.UUID(trip_id))
        if trip_row is not None:
            trip_row.status = TripStatus.FAILED
        await log_stage_event(
            session, trip_id, "run_trip_planning_exhausted", payload={"error": error}
        )
        await session.commit()


class _TripPlanningTask(Task):  # type: ignore[type-arg]
    """Custom Task class so a FINAL failure (all retries exhausted) is
    recorded on the Trip row itself -- on_failure() only fires once,
    after retries stop, not on every intermediate retry (those trigger
    on_retry() instead), so this can't spuriously mark a trip FAILED
    while a retry is still about to succeed.

    NOT `Task[Any, Any]` here -- celery-types' generic Task only exists in
    the type stubs for mypy; the real runtime celery.Task class isn't
    actually subscriptable, and doing so raises TypeError at import time
    (verified live -- a real worker crash, not a hypothetical). The
    `Task[Any, Any]` annotation is still used elsewhere (e.g. `self:
    Task[Any, Any]` on run_trip_planning below) since those are function
    annotations, made lazy strings by `from __future__ import annotations`
    at the top of this file, and never actually evaluated at runtime."""

    def on_failure(
        self,
        exc: BaseException,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: object,
    ) -> None:
        trip_id = args[0] if args else kwargs.get("trip_id")
        if trip_id:
            asyncio.run(_mark_trip_failed(trip_id, str(exc)))


@celery_app.task(
    name="run_trip_planning",
    base=_TripPlanningTask,
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def run_trip_planning(self: Task[Any, Any], trip_id: str) -> str:
    """Runs TripPlanningFlow's research->plan chain for an EXISTING trip
    (trip_id must already exist as a Trip row -- see flow.py's research()
    docstring for why this task never creates one itself). On any
    exception, Celery retries with exponential backoff up to 3 times; each
    retry re-enters research()/plan(), which check AuditLog first and skip
    whatever already completed rather than redoing it (see
    app/tools/audit.py) -- so a retry after a transient provider rate
    limit resumes from where it actually failed, not from scratch. If all
    retries are exhausted, _TripPlanningTask.on_failure() above marks the
    trip FAILED.

    Deliberately synchronous (not `async def`) -- Celery's default worker
    doesn't run its own asyncio event loop, and CrewAI's Flow.kickoff()
    already manages its own event loop internally (verified: it calls
    asyncio.run() itself under the hood), so this can call it directly
    without an extra asyncio.run() wrapper here."""
    flow = TripPlanningFlow()
    flow.kickoff(inputs={"trip_id": trip_id})
    if flow.state.error:
        raise RuntimeError(flow.state.error)
    return trip_id
