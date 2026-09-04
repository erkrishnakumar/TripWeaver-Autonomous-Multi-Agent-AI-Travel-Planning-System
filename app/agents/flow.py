"""
flow.py — the CrewAI Flow wrapping research + planning, and enforcing the
human approval gate (propose -> wait for human -> confirm/reject).

THE CORE SAFETY DECISION IN THIS FILE:
create_trip() and propose_booking() are called as PLAIN PYTHON here, never
as agent tools. The LLM agents (researcher, planner) never touch the
database and never decide when a booking gets proposed. Only this Flow —
ordinary, deterministic Python code — calls those functions, and only
after an explicit human approval step. This is a deliberate, permanent
architectural boundary: giving an LLM direct tool access to trip/booking
creation would mean the LLM decides when a DB write happens, which is
exactly the kind of autonomy this project's "mandatory human-approval gate
before anything is ever actually booked" principle exists to prevent —
see app/agents/tools.py's module docstring for the tool-layer side of this
same decision.

CLI-ONLY DEMO PATH, NOT THE REAL APPROVAL TRIGGER:
wait_for_human_approval() below uses a blocking CLI input() prompt. This
was originally a Phase 8 placeholder for the real approval mechanism; Phase
8 has since shipped (POST /approvals/{id}/confirm|reject, see app/api/main.py
and docs/TripWeaver_Roadmap.md), so this method now only matters for the
standalone CLI entrypoint (`uv run python -m app.agents.flow`) — the real,
production trigger is the API endpoint calling confirm_booking()/
reject_booking() directly (app/tools/confirm_booking.py), never this
method. Kept for local-dev/demo use, not deleted, since the gate itself
(propose, then wait for a separate human decision) remains permanent
regardless of which surface triggers it.

STILL NEVER BOOKS ANYTHING FOR REAL: even after human approval, this Flow
only calls propose_booking() — which writes PENDING_APPROVAL rows, never a
real provider booking endpoint (see app/tools/propose_booking.py). This
Flow's "approval" is approval to PROPOSE a booking for a second, separate,
human-triggered confirmation later (POST /approvals/{id}/confirm) — not
approval to book.

HALLUCINATION GUARD, NOT JUST A PRICE REFRESH: propose_bookings() below
NEVER proposes a flight/hotel/car rental using the FlightOffer/HotelListing/
CarRateOption object the LLM claims it selected. It always re-fetches that
exact offer/listing/rate by ID from the real provider first
(get_flight_offer/get_hotel_rate/get_car_rental_quote) and proposes THAT
verified result instead. This isn't just about getting a fresher price —
it's the only thing standing between a hallucinated, fabricated selection
(a plausible-looking but fake offer_id/search_result_id/rate_id an LLM
invented instead of copying a real one from its own tool results) and that
fake selection silently becoming a real PENDING_APPROVAL row in the
database. A fabricated ID gets a real 404 from the provider and is
rejected here — propose_booking() itself has no way to detect this on its
own, since it just persists whatever object it's handed.
"""

from __future__ import annotations

import asyncio
import re
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from crewai.events import (
    AgentExecutionStartedEvent,
    ToolUsageErrorEvent,
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
    crewai_event_bus,
)
from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel, Field, ValidationError

from app.agents.budget import validate_budget
from app.agents.crew import RESEARCH_TASK_NAMES, build_planning_crew, build_research_crew
from app.agents.schemas import BudgetCheckResult, PlanOutput, ResearchOutput
from app.db.models import Trip
from app.db.models.enums import TripStatus
from app.db.session import get_session
from app.tools.audit import get_last_completed_payload, log_stage_event
from app.tools.car_rentals import get_car_rental_quote
from app.tools.create_trip import create_trip
from app.tools.flights import get_flight_offer
from app.tools.hotels import get_hotel_rate
from app.tools.propose_booking import propose_booking
from app.tools.schemas import (
    CarQuoteInput,
    DriverDetails,
    ProposeBookingResult,
    ProposeCarBookingInput,
    ProposeFlightBookingInput,
    ProposeHotelBookingInput,
    ToolError,
)

if TYPE_CHECKING:
    from crewai.crews.crew_output import CrewOutput
    from crewai.tasks.task_output import TaskOutput


def _truncate_text(value: Any, limit: int = 2000) -> str:
    """Renders a value as a display string capped at `limit` chars -- a
    tool's raw output/error can be many KB (a full flight-search JSON
    blob, e.g.) and would otherwise bloat the audit_log table storing
    what's meant to be a live activity feed, not the system of record for
    the underlying data (the real option/booking rows created later are)."""
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TripPlanningState(BaseModel):
    """Flow state. Populated initially via kickoff(inputs={...}) with the
    request fields (origin_iata..requester_email); everything else fills
    in as the Flow progresses.

    Dates are kept as ISO strings (not `date`) here deliberately — Flow's
    inputs-merge-into-state behavior was verified for str/int/float
    fields; date-typed field coercion through that path wasn't, and
    getting this wrong would silently corrupt trip dates. Parsed to a
    real `date` only at the point create_trip() is actually called.
    """

    origin_iata: str = ""
    destination_iata: str = ""
    depart_date: str = ""
    return_date: str | None = None
    adults: int = 1
    max_budget_usd: float | None = None
    requester_email: str | None = None

    # No explicit signal in the trip request otherwise gives the researcher
    # any reason to select a car rental (it's told to leave it null unless
    # actually useful) -- set this to deliberately request one, e.g. to
    # exercise/test that path.
    wants_car_rental: bool = False

    # TESTING ONLY -- never set these for a real trip. Duffel's Cars sandbox
    # has NO car rental inventory at any real city (verified live: 15
    # different major cities, including the trip's actual destination, all
    # returned zero rates). It only has test inventory at one fixed dummy
    # coordinate, confirmed working live (7 real rates returned, e.g.
    # "Successful Prepaid Booking or similar" -- clearly a Duffel test
    # fixture, not a real place). This lets a demo/test run point the car
    # rental search at that coordinate instead of the trip's real
    # destination, so the booking path can be exercised end-to-end with
    # genuine (not fabricated) sandbox data, while flight/hotel still search
    # the real destination normally. Leave both None for real usage --
    # car rental will then correctly come back null wherever Duffel's
    # sandbox has no inventory, which today is everywhere except this point.
    car_rental_sandbox_test_latitude: float | None = None
    car_rental_sandbox_test_longitude: float | None = None

    # Driver PII, required by Duffel's real /cars/bookings contract but
    # never needed by search/quote. Optional at the Flow level since a car
    # rental itself is optional — only required if the researcher actually
    # selects one AND the human wants it proposed for booking. See
    # DriverDetails' own docstring in app/tools/schemas.py for why this is
    # more sensitive data than anything else this Flow collects.
    driver_given_name: str | None = None
    driver_family_name: str | None = None
    driver_date_of_birth: str | None = None
    driver_email: str | None = None
    driver_phone_number: str | None = None

    trip_id: str = ""
    research_output: ResearchOutput | None = None
    budget_check: BudgetCheckResult | None = None
    plan: PlanOutput | None = None
    approved: bool = False
    flight_booking: ProposeBookingResult | None = None
    hotel_booking: ProposeBookingResult | None = None
    car_rental_bookings: list[ProposeBookingResult] = Field(default_factory=list)
    error: str | None = None


class TripPlanningFlow(Flow[TripPlanningState]):
    def _effective_hotel_checkout(self) -> tuple[str, bool]:
        """Returns (checkout_date_iso, was_defaulted).

        search_hotels() requires a check_out date to compute nights (see
        app/tools/hotels.py) — with no return_date on the trip request, the
        researcher previously had NO checkout date to search with at all,
        which is exactly why a real run came back with selected_hotel=null
        ("a return date is required"). That's a missing-input problem, not
        something a better/different LLM can reason around: the researcher
        can't invent a checkout date any more reliably than it can invent a
        flight offer_id. So default to a 1-night stay here, in plain Python,
        whenever no return_date is given, and say so explicitly in the trip
        summary text below so the researcher's hotel search always has a
        real date to work with and the traveler is told it was assumed."""
        from datetime import date as date_cls
        from datetime import timedelta

        if self.state.return_date:
            return self.state.return_date, False
        depart = date_cls.fromisoformat(self.state.depart_date)
        return (depart + timedelta(days=1)).isoformat(), True

    def _trip_request_summary(self) -> str:
        s = self.state
        checkout_date, checkout_was_defaulted = self._effective_hotel_checkout()
        parts = [
            f"{s.origin_iata} -> {s.destination_iata}",
            f"departing {s.depart_date}",
        ]
        if s.return_date:
            parts.append(f"returning {s.return_date}")
        parts.append(f"{s.adults} adult(s)")
        if s.max_budget_usd is not None:
            parts.append(f"budget ${s.max_budget_usd:.2f}")
        if s.wants_car_rental:
            parts.append("traveler has explicitly requested a car rental")
        if checkout_was_defaulted:
            parts.append(
                f"hotel check-in {s.depart_date}, check-out {checkout_date} "
                "(no return date was given, so this is a DEFAULTED 1-night stay "
                "— use these exact dates for the hotel search, and note in your "
                "findings that the stay length was assumed, not requested)"
            )
        else:
            parts.append(f"hotel check-in {s.depart_date}, check-out {checkout_date}")
        return ", ".join(parts)

    def _car_rental_sandbox_override_note(self) -> str | None:
        """Returns a car-rental-task-only instruction pointing the search at
        Duffel's fixed sandbox test coordinate instead of the trip's real
        destination -- see car_rental_sandbox_test_latitude/longitude's
        docstring on TripPlanningState for why this exists and why it's
        TESTING ONLY. Kept separate from _trip_request_summary() (which
        every gather task shares) so this doesn't leak into flight/hotel/
        context tasks that have no use for it."""
        s = self.state
        if (
            s.car_rental_sandbox_test_latitude is None
            or s.car_rental_sandbox_test_longitude is None
        ):
            return None
        return (
            "TESTING OVERRIDE: for this search specifically, use pickup_latitude="
            f"{s.car_rental_sandbox_test_latitude}, pickup_longitude="
            f"{s.car_rental_sandbox_test_longitude} (and the same for dropoff) "
            "instead of the trip's real destination — this is a fixed Duffel "
            "sandbox test coordinate known to have real test inventory, used "
            "here only to validate the booking path end-to-end with real "
            "(not fabricated) sandbox data, since Duffel's sandbox has no car "
            "rental inventory at any real city. Still report the rate exactly "
            "as the tool returns it, with no invented values."
        )

    def _make_research_progress_callback(
        self, loop: asyncio.AbstractEventLoop
    ) -> Callable[[TaskOutput], None]:
        """Builds the sync task_callback build_research_crew() invokes after
        each of the 5 research Tasks finishes -- so a trip stuck in
        "researching" for a while shows which sub-step it's actually on
        (flight/hotel/car rental/context/format) instead of one opaque
        spinner. See RESEARCH_TASK_NAMES/each Task's name= in crew.py for
        why TaskOutput.name is the only reliable way to tell them apart.

        CrewAI's kickoff_async() runs the whole (synchronous) crew on a
        worker thread via asyncio.to_thread() -- this callback fires on
        THAT thread, not the event loop driving this coroutine, so it can't
        just `await log_stage_event(...)` directly. run_coroutine_threadsafe
        schedules the write back onto the original loop and blocks this
        worker thread until it's actually committed, preserving the same
        ordering guarantee log_stage_event()'s own sequence numbering
        depends on (see its docstring)."""

        def _on_task_complete(output: TaskOutput) -> None:
            if output.name not in RESEARCH_TASK_NAMES:
                return

            async def _write() -> None:
                async with get_session() as session:
                    await log_stage_event(
                        session, self.state.trip_id, f"research.{output.name}_completed"
                    )
                    await session.commit()

            try:
                asyncio.run_coroutine_threadsafe(_write(), loop).result(timeout=10)
            except Exception:  # noqa: BLE001 -- a progress-log hiccup must never fail research itself
                pass

        return _on_task_complete

    @contextmanager
    def _capture_agent_activity(self, loop: asyncio.AbstractEventLoop) -> Iterator[None]:
        """Subscribes to CrewAI's event bus for the duration of ONE
        crew.kickoff_async() call, writing a live "agent.*" AuditLog row
        for every agent-started / tool-started / tool-finished / tool-error
        event -- this is what lets the frontend's Activity feed show which
        specific agent/tool is running right now, not just the coarse
        research.*_completed milestones _make_research_progress_callback()
        above already logs. Same asyncio.run_coroutine_threadsafe bridge as
        that method -- see its docstring for why a sync CrewAI callback
        can't just `await` the write directly.

        Registered/unregistered around each crew.kickoff_async() call, not
        once per process: crewai_event_bus is a process-wide singleton, so
        a handler left registered after this trip's crew finishes would
        keep firing -- and double-firing on the next registration -- for
        every subsequent trip this same worker ever processes. Flushes the
        bus before unregistering -- verified live that emit() dispatches
        sync handlers on a background ThreadPoolExecutor and returns
        immediately, so skipping the flush could silently drop the last
        event or two emitted right as the crew finishes.
        """

        def _write(event_type: str, payload: dict[str, Any]) -> None:
            async def _do_write() -> None:
                async with get_session() as session:
                    await log_stage_event(session, self.state.trip_id, event_type, payload=payload)
                    await session.commit()

            try:
                asyncio.run_coroutine_threadsafe(_do_write(), loop).result(timeout=10)
            except Exception:  # noqa: BLE001 -- a progress-log hiccup must never fail the real run
                pass

        def _on_agent_started(source: Any, event: AgentExecutionStartedEvent) -> None:
            _write(
                "agent.started",
                {
                    "agent_role": event.agent.role,
                    "task_name": getattr(event.task, "name", None)
                    or getattr(event.task, "description", None),
                },
            )

        def _on_tool_started(source: Any, event: ToolUsageStartedEvent) -> None:
            _write(
                "agent.tool_started",
                {
                    "agent_role": event.agent_role,
                    "tool_name": event.tool_name,
                    "tool_args": event.tool_args
                    if isinstance(event.tool_args, dict)
                    else _truncate_text(event.tool_args),
                },
            )

        def _on_tool_finished(source: Any, event: ToolUsageFinishedEvent) -> None:
            _write(
                "agent.tool_completed",
                {
                    "agent_role": event.agent_role,
                    "tool_name": event.tool_name,
                    "output": _truncate_text(event.output),
                },
            )

        def _on_tool_error(source: Any, event: ToolUsageErrorEvent) -> None:
            _write(
                "agent.tool_failed",
                {
                    "agent_role": event.agent_role,
                    "tool_name": event.tool_name,
                    "error": _truncate_text(event.error),
                },
            )

        handlers: list[tuple[type, Any]] = [
            (AgentExecutionStartedEvent, _on_agent_started),
            (ToolUsageStartedEvent, _on_tool_started),
            (ToolUsageFinishedEvent, _on_tool_finished),
            (ToolUsageErrorEvent, _on_tool_error),
        ]
        for event_type, handler in handlers:
            crewai_event_bus.on(event_type)(handler)
        try:
            yield
        finally:
            # crewai_event_bus.emit() dispatches sync handlers on a
            # background ThreadPoolExecutor and returns immediately --
            # verified live, not assumed: without this flush, an event
            # emitted right before crew.kickoff_async() returns could
            # still be queued (not yet run) when .off() below removes the
            # handler, silently dropping that last activity-log write.
            crewai_event_bus.flush(timeout=10)
            for event_type, handler in handlers:
                crewai_event_bus.off(event_type, handler)

    @start()
    async def research(self) -> ResearchOutput:
        """Kick off the research Crew -- now resumability-aware: if this
        trip already has a completed research stage logged (a prior
        attempt got this far before failing later), skip straight to
        using that instead of re-running the crew and burning LLM calls
        again. See app/agents/audit.py for the full rationale.

        Also auto-creates the Trip row if trip_id wasn't already provided
        by the caller -- preserves the old persist_draft_trip() behavior
        for the CLI demo entrypoint, while letting a future Celery/API
        caller pass an existing trip_id instead (skipping creation,
        enabling resume)."""
        async with get_session() as session:
            if not self.state.trip_id:
                trip = await create_trip(
                    session,
                    origin_iata=self.state.origin_iata,
                    destination_iata=self.state.destination_iata,
                    depart_date=date_cls.fromisoformat(self.state.depart_date),
                    return_date=(
                        date_cls.fromisoformat(self.state.return_date)
                        if self.state.return_date
                        else None
                    ),
                    adults=self.state.adults,
                    max_budget_usd=self.state.max_budget_usd,
                    requester_email=self.state.requester_email,
                    wants_car_rental=self.state.wants_car_rental,
                )
                await session.commit()
                self.state.trip_id = str(trip.id)
                trip_row: Trip | None = trip
            else:
                # trip_id was pre-supplied (the Celery/API path) -- load the
                # canonical request fields from the DB row instead of
                # requiring the caller to re-pass everything that was
                # already persisted when the trip was created. driver_*/
                # sandbox-test fields are NOT loaded here -- they're only
                # used later by propose_bookings(), never by research()/
                # plan(), and deliberately don't live on the Trip row (see
                # Trip.wants_car_rental's own comment).
                trip_row = await session.get(Trip, uuid.UUID(self.state.trip_id))
                if trip_row is not None:
                    self.state.origin_iata = trip_row.origin_iata
                    self.state.destination_iata = trip_row.destination_iata
                    self.state.depart_date = trip_row.depart_date.isoformat()
                    self.state.return_date = (
                        trip_row.return_date.isoformat() if trip_row.return_date else None
                    )
                    self.state.adults = trip_row.adults
                    self.state.max_budget_usd = trip_row.max_budget_usd
                    self.state.requester_email = trip_row.requester_email
                    self.state.wants_car_rental = trip_row.wants_car_rental

            existing = await get_last_completed_payload(session, self.state.trip_id, "research")
            if existing is not None:
                research_output = ResearchOutput.model_validate(existing)
                self.state.research_output = research_output
                return research_output

            if trip_row is not None:
                trip_row.status = TripStatus.RESEARCHING
            await log_stage_event(session, self.state.trip_id, "research_started")
            await session.commit()

        crew = build_research_crew(
            self._trip_request_summary(),
            car_rental_override=self._car_rental_sandbox_override_note(),
            task_callback=self._make_research_progress_callback(asyncio.get_running_loop()),
        )
        try:
            with self._capture_agent_activity(asyncio.get_running_loop()):
                crew_output = cast("CrewOutput", await crew.kickoff_async())
            research_output = cast(ResearchOutput, crew_output.pydantic)
            assert isinstance(research_output, ResearchOutput)
        except (ValidationError, AssertionError) as e:
            self.state.error = f"Research step produced malformed output: {e}"
            research_output = ResearchOutput()
        except Exception as e:  # noqa: BLE001 -- provider rate limits etc., see below
            self.state.error = f"Research step failed: {e}"
            research_output = ResearchOutput()

        async with get_session() as session:
            if self.state.error:
                await log_stage_event(
                    session,
                    self.state.trip_id,
                    "research_failed",
                    payload={"error": self.state.error},
                )
            else:
                await log_stage_event(
                    session,
                    self.state.trip_id,
                    "research_completed",
                    payload=research_output.model_dump(mode="json"),
                )
            await session.commit()

        self.state.research_output = research_output
        return research_output

    @listen(research)
    async def check_budget(self, research_output: ResearchOutput) -> BudgetCheckResult:
        """Plain Python, no LLM -- see app/agents/budget.py for why."""
        budget_check = validate_budget(
            max_budget_usd=self.state.max_budget_usd,
            flight=research_output.selected_flight,
            hotel=research_output.selected_hotel,
            car_rentals=research_output.selected_car_rentals,
        )
        self.state.budget_check = budget_check
        async with get_session() as session:
            await log_stage_event(
                session,
                self.state.trip_id,
                "budget_checked",
                payload=budget_check.model_dump(mode="json"),
            )
            await session.commit()
        return budget_check

    @listen(check_budget)
    async def plan(self, budget_check: BudgetCheckResult) -> PlanOutput:
        """Same resumability pattern as research() -- see that method's
        docstring."""
        async with get_session() as session:
            existing = await get_last_completed_payload(session, self.state.trip_id, "plan")
            if existing is not None:
                plan_output = PlanOutput.model_validate(existing)
                self.state.plan = plan_output
                return plan_output

            trip_row = await session.get(Trip, uuid.UUID(self.state.trip_id))
            if trip_row is not None:
                trip_row.status = TripStatus.PLANNING
            await log_stage_event(session, self.state.trip_id, "plan_started")
            await session.commit()

        crew = build_planning_crew(
            self._trip_request_summary(),
            self.state.research_output,
            budget_check,
        )
        try:
            with self._capture_agent_activity(asyncio.get_running_loop()):
                crew_output = cast("CrewOutput", await crew.kickoff_async())
            plan_output = cast(PlanOutput, crew_output.pydantic)
            assert isinstance(plan_output, PlanOutput)
        except (ValidationError, AssertionError) as e:
            self._add_error(f"Planning step produced malformed output: {e}")
            plan_output = PlanOutput(itinerary_summary="(plan could not be generated)")
        except Exception as e:  # noqa: BLE001
            self._add_error(f"Planning step failed: {e}")
            plan_output = PlanOutput(itinerary_summary="(plan could not be generated)")

        async with get_session() as session:
            if self.state.error:
                await log_stage_event(
                    session, self.state.trip_id, "plan_failed", payload={"error": self.state.error}
                )
            else:
                await log_stage_event(
                    session,
                    self.state.trip_id,
                    "plan_completed",
                    payload=plan_output.model_dump(mode="json"),
                )
            await session.commit()

        self.state.plan = plan_output
        return plan_output

    @listen(plan)
    async def mark_awaiting_approval(self, plan_output: PlanOutput) -> str:
        """Automated chain stops HERE. Deliberately does NOT auto-call
        wait_for_human_approval()/propose_bookings() anymore -- a Celery
        worker has no terminal to block on input() with, and a real
        approval has to come from an HTTP call to a separate endpoint,
        not from this Flow completing. Sets the trip's final pre-approval
        status: AWAITING_APPROVAL if research+plan both genuinely
        succeeded, FAILED if either one degraded (see research()/plan()'s
        error handling) -- there's nothing meaningful to approve if the
        plan itself couldn't be produced."""
        async with get_session() as session:
            trip_row = await session.get(Trip, uuid.UUID(self.state.trip_id))
            if trip_row is not None:
                trip_row.status = (
                    TripStatus.FAILED if self.state.error else TripStatus.AWAITING_APPROVAL
                )
            await session.commit()
        return cast(str, self.state.trip_id)

    def wait_for_human_approval(self, trip_id: str) -> bool:
        """*** CLI-ONLY DEMO PATH — SEE MODULE DOCSTRING ***

        Blocking CLI prompt used only by the standalone CLI entrypoint; the
        real, production approval trigger is POST /approvals/{id}/confirm|
        reject (see app/api/main.py), which never calls this method. Shows
        the plan and budget verdict, and requires an explicit 'y' to
        proceed — anything else (including just pressing enter) is treated
        as a rejection, not a default approval, since a human-approval gate
        that defaults to "yes" isn't one.
        """
        plan = self.state.plan
        budget = self.state.budget_check

        print("\n" + "=" * 70)
        print(f"TRIP PLAN FOR APPROVAL (trip_id={trip_id})")
        print("=" * 70)
        print(plan.itinerary_summary if plan else "(no plan produced)")
        if plan and plan.day_by_day:
            print("\nDay by day:")
            for line in plan.day_by_day:
                print(f"  - {line}")
        if plan and plan.caveats:
            print("\nCaveats:")
            for c in plan.caveats:
                print(f"  ! {c}")
        if budget:
            print(f"\nBudget: {budget.message}")
        print("=" * 70)

        answer = input("Approve this plan and propose bookings? [y/N]: ").strip().lower()
        approved = answer == "y"
        self.state.approved = approved
        return approved

    def _add_error(self, message: str) -> None:
        """Appends rather than overwrites, so one failed proposal (e.g.
        flight) doesn't silently discard an earlier one (e.g. hotel) — the
        original code overwrote self.state.error per branch, which could
        drop a real failure from the final state."""
        self.state.error = f"{self.state.error} {message}" if self.state.error else message

    async def propose_bookings(self, approved: bool) -> None:
        """Only reachable after an explicit human 'y'. Writes
        PENDING_APPROVAL rows via propose_booking() — still NEVER a real
        booking; see module docstring."""
        if not approved:
            # _add_error(), not a direct assignment -- found live while
            # writing the Phase 7 evals: this used to overwrite (not
            # append to) an earlier real error (e.g. "Research step
            # failed: <rate limit>"), silently discarding it the moment a
            # human rejected the resulting bad plan, which is exactly the
            # failure mode _add_error() exists to prevent (see its own
            # docstring above).
            self._add_error("Plan was not approved by the human reviewer.")
            return

        research_output = self.state.research_output
        if research_output is None:
            self.state.error = "No research output available to propose bookings from."
            return

        async with get_session() as session:
            if research_output.selected_flight is not None:
                # Re-fetch by ID rather than trusting the LLM-selected
                # object directly — see module docstring's HALLUCINATION
                # GUARD note. A fabricated offer_id fails here with a real
                # 404, before anything is written to the database.
                verified_flight = get_flight_offer(research_output.selected_flight.offer_id)
                if isinstance(verified_flight, ToolError):
                    self._add_error(f"Flight verification failed: {verified_flight.message}")
                else:
                    flight_result = await propose_booking(
                        session,
                        ProposeFlightBookingInput(
                            trip_id=self.state.trip_id,
                            offer=verified_flight,
                        ),
                    )
                    if isinstance(flight_result, ToolError):
                        self._add_error(f"Flight proposal failed: {flight_result.message}")
                        await session.rollback()
                    else:
                        self.state.flight_booking = flight_result
                        await session.commit()

            if research_output.selected_hotel is not None:
                from datetime import date as date_cls

                check_in = date_cls.fromisoformat(self.state.depart_date)
                check_out = (
                    date_cls.fromisoformat(self.state.return_date)
                    if self.state.return_date
                    else check_in
                )
                nights = max((check_out - check_in).days, 1)

                # Same hallucination guard as flights — re-fetch by ID
                # rather than trusting the LLM-selected object directly.
                verified_hotel = get_hotel_rate(
                    research_output.selected_hotel.search_result_id, nights
                )
                if isinstance(verified_hotel, ToolError):
                    self._add_error(f"Hotel verification failed: {verified_hotel.message}")
                else:
                    hotel_result = await propose_booking(
                        session,
                        ProposeHotelBookingInput(
                            trip_id=self.state.trip_id,
                            listing=verified_hotel,
                            check_in=check_in,
                            check_out=check_out,
                        ),
                    )
                    if isinstance(hotel_result, ToolError):
                        self._add_error(f"Hotel proposal failed: {hotel_result.message}")
                        await session.rollback()
                    else:
                        self.state.hotel_booking = hotel_result
                        await session.commit()

            for car_rate in research_output.selected_car_rentals:
                driver_fields = (
                    self.state.driver_given_name,
                    self.state.driver_family_name,
                    self.state.driver_date_of_birth,
                    self.state.driver_email,
                    self.state.driver_phone_number,
                )
                if not all(driver_fields):
                    self._add_error(
                        f"A car rental ({car_rate.pickup_location_name} -> "
                        f"{car_rate.dropoff_location_name}) was selected but driver "
                        "details were not provided, so it could not be proposed for "
                        "booking."
                    )
                    continue

                from datetime import date as date_cls

                assert self.state.driver_given_name is not None
                assert self.state.driver_family_name is not None
                assert self.state.driver_date_of_birth is not None
                assert self.state.driver_email is not None
                assert self.state.driver_phone_number is not None

                # Re-fetch a FIRM quote here rather than trusting the
                # research step's rate estimate — Duffel's own docs warn
                # the quote price can differ from the rate's price, and
                # a quote can expire between research and this step.
                quote = get_car_rental_quote(CarQuoteInput(rate_id=car_rate.rate_id))
                if isinstance(quote, ToolError):
                    self._add_error(
                        f"Car rental quote failed for {car_rate.pickup_location_name} "
                        f"-> {car_rate.dropoff_location_name}: {quote.message}"
                    )
                    continue

                car_result = await propose_booking(
                    session,
                    ProposeCarBookingInput(
                        trip_id=self.state.trip_id,
                        rate=car_rate,
                        quote=quote,
                        driver=DriverDetails(
                            given_name=self.state.driver_given_name,
                            family_name=self.state.driver_family_name,
                            date_of_birth=date_cls.fromisoformat(self.state.driver_date_of_birth),
                            email=self.state.driver_email,
                            phone_number=self.state.driver_phone_number,
                        ),
                    ),
                )
                if isinstance(car_result, ToolError):
                    self._add_error(f"Car rental proposal failed: {car_result.message}")
                    await session.rollback()
                else:
                    self.state.car_rental_bookings.append(car_result)
                    await session.commit()

        if (
            self.state.flight_booking is None
            and self.state.hotel_booking is None
            and len(self.state.car_rental_bookings) == 0
            and self.state.error is None
        ):
            self.state.error = (
                "Plan was approved, but no flight, hotel, or car rental was selected "
                "during research, so nothing was proposed for booking."
            )


def run_trip_planning_flow(
    *,
    origin_iata: str,
    destination_iata: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 1,
    max_budget_usd: float | None = None,
    requester_email: str | None = None,
    wants_car_rental: bool = False,
    driver_given_name: str | None = None,
    driver_family_name: str | None = None,
    driver_date_of_birth: str | None = None,
    driver_email: str | None = None,
    driver_phone_number: str | None = None,
    car_rental_sandbox_test_latitude: float | None = None,
    car_rental_sandbox_test_longitude: float | None = None,
) -> TripPlanningState:
    """Entrypoint: run `uv run python -m app.agents.flow` or import this
    directly. Dates are ISO strings ("2026-09-14") — see
    TripPlanningState's docstring for why.

    wants_car_rental defaults to False -- the researcher is instructed to
    leave selected_car_rentals empty unless a car rental is actually useful
    for the trip, and nothing else in the request signals that, so without
    this flag the researcher has no reason to ever select one. Set it to
    True to deliberately request one (e.g. to test that path end to end).

    driver_* fields are only needed if you want a researcher-selected car
    rental to actually get proposed for booking — see propose_bookings()'s
    module docstring. Leave them unset if you don't expect (or don't want)
    a car rental proposal; a selected car rental without driver details
    just surfaces an informational note in the final state instead of
    failing the whole Flow.

    car_rental_sandbox_test_latitude/longitude are TESTING ONLY -- see
    TripPlanningState's docstring for the full explanation. Leave both None
    for real usage.
    """
    flow = TripPlanningFlow()
    flow.kickoff(
        inputs={
            "origin_iata": origin_iata,
            "destination_iata": destination_iata,
            "depart_date": depart_date,
            "return_date": return_date,
            "adults": adults,
            "max_budget_usd": max_budget_usd,
            "requester_email": requester_email,
            "wants_car_rental": wants_car_rental,
            "driver_given_name": driver_given_name,
            "driver_family_name": driver_family_name,
            "driver_date_of_birth": driver_date_of_birth,
            "car_rental_sandbox_test_latitude": car_rental_sandbox_test_latitude,
            "car_rental_sandbox_test_longitude": car_rental_sandbox_test_longitude,
            "driver_email": driver_email,
            "driver_phone_number": driver_phone_number,
        }
    )

    if flow.state.error is None:
        approved = flow.wait_for_human_approval(flow.state.trip_id)
        asyncio.run(flow.propose_bookings(approved))

    return cast(TripPlanningState, flow.state)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class _AnsiStrippingWriter:
    """Wraps a writable stream and strips ANSI escape codes before writing.
    Rich writes raw color/style codes to whatever stream it's given -- a
    real terminal renders those as color, but a plain text file just stores
    them literally (e.g. '[34m', '[0m'), making the saved log file far
    harder to read than the live terminal output it's meant to preserve.
    Used to wrap ONLY the log-file stream passed to _TeeStream below, never
    the real terminal stream, so you keep colors on screen."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, data: str) -> None:
        self._stream.write(_ANSI_ESCAPE_RE.sub("", data))

    def flush(self) -> None:
        self._stream.flush()


class _TeeStream:
    """Mirrors every write to N underlying streams (e.g. the real console
    AND a log file) so a run's full output is both visible live and saved
    for later, without the caller having to remember to redirect (`*>` /
    Tee-Object) by hand every time. Proxies unknown attributes (isatty,
    encoding, ...) to the first stream so Rich's terminal-capability
    detection still sees the real console, not the log file."""

    def __init__(self, *streams: Any) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for s in self._streams:
            s.write(data)

    def flush(self) -> None:
        for s in self._streams:
            s.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._streams[0], name)


@contextmanager
def capture_output_to_log_file(label: str) -> Iterator[Path]:
    """Redirects stdout/stderr to BOTH the real stream and a timestamped
    log file under logs/ (ANSI codes stripped from the file copy only --
    see _AnsiStrippingWriter), restoring the originals on exit. Shared by
    the CLI entrypoint below AND run_trip_planning() (the Celery task) --
    without this, a worker-driven run's Rich output only ever appears in
    the worker process's own console, never saved anywhere (verified
    live: exactly why Celery-driven runs weren't landing in logs/ the way
    CLI runs always have)."""
    # getattr(..., None), not sys.stdout.encoding directly -- Celery
    # workers wrap stdout/stderr in their own logging proxy (routes
    # print() through Celery's logging system), and that proxy has no
    # .encoding attribute at all (verified live: a real worker crash,
    # AttributeError: 'LoggingProxy' object has no attribute 'encoding').
    # If we can't even tell the current encoding, don't attempt to
    # reconfigure it -- this whole UTF-8 fix is only relevant for a real
    # console anyway.
    stdout_encoding = getattr(sys.stdout, "encoding", None)
    if stdout_encoding and stdout_encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"{label}.log"
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")
    log_file.write(f"\n\n===== Attempt started {datetime.now():%Y-%m-%d %H:%M:%S} =====\n\n")
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(orig_stdout, _AnsiStrippingWriter(log_file))
    sys.stderr = _TeeStream(orig_stderr, _AnsiStrippingWriter(log_file))
    print(f"Logging full output to {log_path}")
    try:
        yield log_path
    finally:
        sys.stdout, sys.stderr = orig_stdout, orig_stderr
        log_file.close()


if __name__ == "__main__":
    from datetime import date, timedelta

    from app.logging_config import configure_logging

    configure_logging()

    # Windows' default console codepage (cp1252) can't encode the emoji in
    # CrewAI's Rich-based progress panels -- verified live: running this
    # script with stdout redirected to a file (not an interactive terminal)
    # raised 'charmap' codec can't encode character ... for every panel
    # containing one, and CrewAI's event bus swallows that error per-handler
    # rather than crashing, which silently drops most of the run's actual
    # tool/task/agent output instead of failing loudly. Forcing UTF-8 here
    # (errors="replace" so a still-unencodable byte degrades to a
    # placeholder glyph instead of losing the whole panel) fixes this for
    # every invocation of this script, not just ones that happen to set
    # PYTHONIOENCODING=utf-8 themselves.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    # Every run's full console output (all the Rich panels a verbose Crew
    # produces -- easily 1000+ lines) is also saved to a timestamped file
    # under logs/, so a past run can be reviewed later without having to
    # remember to redirect (`*> run.log`) by hand each time, and without
    # losing anything to a terminal's limited scrollback buffer. logs/ is
    # already covered by .gitignore's `*.log` rule.
    logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"flow_run_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    sys.stdout = _TeeStream(sys.stdout, _AnsiStrippingWriter(log_file))
    sys.stderr = _TeeStream(sys.stderr, _AnsiStrippingWriter(log_file))
    print(f"Logging full output to {log_path}")

    final_state = run_trip_planning_flow(
        origin_iata="JFK",
        destination_iata="ATL",
        depart_date=(date.today() + timedelta(days=30)).isoformat(),
        adults=1,
        max_budget_usd=1500.0,
        # Explicitly request a car rental so this manual run actually
        # exercises that path -- without this, the researcher has no
        # signal to ever select one for a plain point-to-point trip.
        wants_car_rental=True,
        # Sample driver details so a researcher-selected car rental can
        # actually be proposed end to end during a manual run -- remove
        # these if you don't want car rental proposals attempted.
        driver_given_name="Jane",
        driver_family_name="Doe",
        driver_date_of_birth="1990-01-01",
        driver_email="jane.doe@example.com",
        driver_phone_number="+15555550100",
        # TESTING ONLY -- see TripPlanningState's docstring. Verified live:
        # Duffel's Cars sandbox has zero rate inventory at any of 15 real
        # cities tried (including this trip's actual destination, Atlanta),
        # but this exact fixed coordinate returned 7 real rates (Duffel's
        # own sandbox test fixture, e.g. "Successful Prepaid Booking or
        # similar"). Pointing the car search here instead of the real
        # destination lets this manual run exercise the full car rental
        # booking path end-to-end with genuine sandbox data. Remove these
        # two lines for a run that reflects real-world car availability
        # (which, for Atlanta today, is correctly null).
        car_rental_sandbox_test_latitude=-24.38,
        car_rental_sandbox_test_longitude=-128.32,
    )
    print("\nFinal state:", final_state.model_dump_json(indent=2))
