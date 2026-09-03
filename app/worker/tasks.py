"""
Celery tasks. ping() is a trivial pipeline-verification task (see its own
docstring). run_trip_planning() wraps TripPlanningFlow's resumable
research->plan chain (app/agents/flow.py) so it can run as a background
job instead of blocking an HTTP request for however long a multi-provider
LLM pipeline takes. propose_trip_bookings() is Gate 1's actual work,
triggered by POST /trips/{id}/proceed -- see docs/TripWeaver_Roadmap.md's
Phase 8 notes for why both of these need to be background jobs rather
than synchronous endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from celery import Task

from app.agents.flow import TripPlanningFlow
from app.agents.schemas import ResearchOutput
from app.tools.audit import get_last_completed_payload, log_stage_event
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


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
    logger.error(
        "run_trip_planning: all retries exhausted, trip marked FAILED: %s",
        error,
        extra={"trip_id": trip_id},
    )


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
    from app.agents.flow import capture_output_to_log_file

    logger.info(
        "run_trip_planning: starting (attempt %d/%d)",
        self.request.retries + 1,
        (self.max_retries or 0) + 1,
        extra={"trip_id": trip_id},
    )
    flow = TripPlanningFlow()
    with capture_output_to_log_file(f"trip_{trip_id}"):
        flow.kickoff(inputs={"trip_id": trip_id})
    if flow.state.error:
        logger.warning(
            "run_trip_planning: failed, will retry if attempts remain: %s",
            flow.state.error,
            extra={"trip_id": trip_id},
        )
        raise RuntimeError(flow.state.error)
    logger.info(
        "run_trip_planning: succeeded, trip is awaiting_approval", extra={"trip_id": trip_id}
    )
    return trip_id


async def _load_flow_state_for_proposal(trip_id: str) -> TripPlanningFlow:
    """Reconstructs a TripPlanningFlow's state for a trip that already
    completed research+plan in a PREVIOUS Celery task (run_trip_planning)
    -- that task's in-memory Flow object is long gone by the time a human
    approves via POST /trips/{id}/proceed, so this rebuilds just enough
    state for propose_bookings() to run: the trip's own fields (from the
    Trip row, same fields research() already knows how to load) and its
    completed research output (from AuditLog's research_completed
    payload -- the same one research()'s resumability check reads)."""
    from app.db.models import Trip
    from app.db.session import get_session

    flow = TripPlanningFlow()
    flow.state.trip_id = trip_id
    async with get_session() as session:
        trip_row = await session.get(Trip, uuid.UUID(trip_id))
        if trip_row is None:
            raise ValueError(f"Trip {trip_id} not found")
        flow.state.origin_iata = trip_row.origin_iata
        flow.state.destination_iata = trip_row.destination_iata
        flow.state.depart_date = trip_row.depart_date.isoformat()
        flow.state.return_date = trip_row.return_date.isoformat() if trip_row.return_date else None
        flow.state.adults = trip_row.adults
        flow.state.max_budget_usd = trip_row.max_budget_usd
        flow.state.requester_email = trip_row.requester_email
        flow.state.wants_car_rental = trip_row.wants_car_rental

        research_payload = await get_last_completed_payload(session, trip_id, "research")
        if research_payload is None:
            raise ValueError(f"Trip {trip_id} has no completed research to propose bookings from")
        flow.state.research_output = ResearchOutput.model_validate(research_payload)

    return flow


class NoBookingsProposedError(RuntimeError):
    """Raised when nothing could be proposed for genuine business reasons
    (expired offers, missing driver details) -- NOT a transient failure.
    Retrying won't help: propose_bookings() re-verifies the SAME already-
    researched offer/rate ids every time, it never re-runs research to
    fetch fresh ones, so an expired offer stays expired no matter how many
    times this retries. Excluded from autoretry via dont_autoretry_for on
    the task below, so this fails fast instead of burning 3 pointless
    retries against data that will never become valid again."""


async def _propose_bookings_for_trip(trip_id: str) -> None:
    from app.db.models import Trip
    from app.db.models.enums import TripStatus
    from app.db.session import get_session

    flow = await _load_flow_state_for_proposal(trip_id)
    await flow.propose_bookings(approved=True)

    nothing_proposed = (
        flow.state.flight_booking is None
        and flow.state.hotel_booking is None
        and flow.state.car_rental_booking is None
    )
    async with get_session() as session:
        trip_row = await session.get(Trip, uuid.UUID(trip_id))
        if trip_row is not None:
            trip_row.status = TripStatus.FAILED if nothing_proposed else TripStatus.APPROVED
        await session.commit()

    if nothing_proposed:
        logger.warning(
            "propose_trip_bookings: nothing could be proposed: %s",
            flow.state.error,
            extra={"trip_id": trip_id},
        )
        raise NoBookingsProposedError(flow.state.error or "No bookings were proposed.")
    logger.info("propose_trip_bookings: succeeded", extra={"trip_id": trip_id})


@celery_app.task(
    name="propose_trip_bookings",
    bind=True,
    autoretry_for=(Exception,),
    dont_autoretry_for=(NoBookingsProposedError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def propose_trip_bookings(self: Task[Any, Any], trip_id: str) -> str:
    """Gate 1's actual work -- triggered by POST /trips/{id}/proceed, the
    literal HTTP replacement for the CLI's blocking input() prompt.
    approved=True is implied by this endpoint having been called at all --
    there's no separate y/n step here, the human's approval already
    happened by making the HTTP call.

    driver_* fields are left unset here, deliberately -- they're not
    persisted on Trip (driver PII is the most sensitive data this project
    stores, see Trip.wants_car_rental's docstring). If the researcher
    selected a car rental, propose_bookings() degrades gracefully without
    them (flight/hotel still get proposed normally) rather than failing
    outright. A future version of this endpoint could accept driver
    details in its request body specifically for a car rental proposal."""
    asyncio.run(_propose_bookings_for_trip(trip_id))
    return trip_id
