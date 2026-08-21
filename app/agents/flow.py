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

PHASE 8 PLACEHOLDER, NOT A REAL APPROVAL MECHANISM:
wait_for_human_approval() below uses a blocking CLI input() prompt. This
is explicitly a local-dev/demo stand-in for the real approval mechanism
that Phase 8's API layer is supposed to provide (a separate, human-
triggered POST /approvals/{id}/confirm|reject endpoint, per the roadmap's
Phase 4 notes). When Phase 8 exists, this method should be replaced with
something that actually waits on that endpoint being called (e.g. polling
the Approval row, or a webhook) — not deleted, since the gate itself must
remain permanent; only its trigger mechanism changes.

STILL NEVER BOOKS ANYTHING FOR REAL: even after human approval, this Flow
only calls propose_booking() — which writes PENDING_APPROVAL rows, never a
real provider booking endpoint (see app/tools/propose_booking.py). This
Flow's "approval" is approval to PROPOSE a booking for a second, separate,
human-triggered confirmation later (Phase 8) — not approval to book.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

from app.agents.budget import validate_budget
from app.agents.crew import build_planning_crew, build_research_crew
from app.agents.schemas import BudgetCheckResult, PlanOutput, ResearchOutput
from app.db.session import get_session
from app.tools.create_trip import create_trip
from app.tools.propose_booking import propose_booking
from app.tools.schemas import (
    ProposeBookingResult,
    ProposeFlightBookingInput,
    ProposeHotelBookingInput,
    ToolError,
)

if TYPE_CHECKING:
    from crewai.crews.crew_output import CrewOutput


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

    trip_id: str = ""
    research_output: ResearchOutput | None = None
    budget_check: BudgetCheckResult | None = None
    plan: PlanOutput | None = None
    approved: bool = False
    flight_booking: ProposeBookingResult | None = None
    hotel_booking: ProposeBookingResult | None = None
    error: str | None = None


class TripPlanningFlow(Flow[TripPlanningState]):
    def _trip_request_summary(self) -> str:
        s = self.state
        parts = [
            f"{s.origin_iata} -> {s.destination_iata}",
            f"departing {s.depart_date}",
        ]
        if s.return_date:
            parts.append(f"returning {s.return_date}")
        parts.append(f"{s.adults} adult(s)")
        if s.max_budget_usd is not None:
            parts.append(f"budget ${s.max_budget_usd:.2f}")
        return ", ".join(parts)

    @start()
    async def research(self) -> ResearchOutput:
        """Kick off the research Crew. Runs the researcher Agent, which
        calls the read-only tools in app/agents/tools.py — no DB, no
        booking, nothing that needs human approval yet."""
        crew = build_research_crew(self._trip_request_summary())
        crew_output = cast("CrewOutput", crew.kickoff())
        research_output = crew_output.pydantic
        assert isinstance(research_output, ResearchOutput)
        self.state.research_output = research_output
        return research_output

    @listen(research)
    def check_budget(self, research_output: ResearchOutput) -> BudgetCheckResult:
        """Plain Python, no LLM — see app/agents/budget.py for why."""
        budget_check = validate_budget(
            max_budget_usd=self.state.max_budget_usd,
            flight=research_output.selected_flight,
            hotel=research_output.selected_hotel,
        )
        self.state.budget_check = budget_check
        return budget_check

    @listen(check_budget)
    def plan(self, budget_check: BudgetCheckResult) -> PlanOutput:
        """Kick off the planning Crew with the research + budget results."""
        crew = build_planning_crew(
            self._trip_request_summary(),
            self.state.research_output,
            budget_check,
        )
        crew_output = cast("CrewOutput", crew.kickoff())
        plan_output = crew_output.pydantic
        assert isinstance(plan_output, PlanOutput)
        self.state.plan = plan_output
        return plan_output

    @listen(plan)
    async def persist_draft_trip(self, plan_output: PlanOutput) -> str:
        """Plain Python DB write — creates the Trip row in DRAFT status so
        there's a trip_id to attach proposed bookings to later. No agent
        involvement; see module docstring."""
        from datetime import date as date_cls

        async with get_session() as session:
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
            )
            await session.commit()
            trip_id = str(trip.id)

        self.state.trip_id = trip_id
        return trip_id

    @listen(persist_draft_trip)
    def wait_for_human_approval(self, trip_id: str) -> bool:
        """*** PHASE 8 PLACEHOLDER — SEE MODULE DOCSTRING ***

        Blocking CLI prompt standing in for a real, separate, human-
        triggered approval mechanism. Shows the plan and budget verdict,
        and requires an explicit 'y' to proceed — anything else (including
        just pressing enter) is treated as a rejection, not a default
        approval, since a human-approval gate that defaults to "yes" isn't
        one.
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

    @listen(wait_for_human_approval)
    async def propose_bookings(self, approved: bool) -> None:
        """Only reachable after an explicit human 'y'. Writes
        PENDING_APPROVAL rows via propose_booking() — still NEVER a real
        booking; see module docstring."""
        if not approved:
            self.state.error = "Plan was not approved by the human reviewer."
            return

        research_output = self.state.research_output
        if research_output is None:
            self.state.error = "No research output available to propose bookings from."
            return

        async with get_session() as session:
            if research_output.selected_flight is not None:
                flight_result = await propose_booking(
                    session,
                    ProposeFlightBookingInput(
                        trip_id=self.state.trip_id,
                        offer=research_output.selected_flight,
                    ),
                )
                if isinstance(flight_result, ToolError):
                    self.state.error = f"Flight proposal failed: {flight_result.message}"
                    await session.rollback()
                else:
                    self.state.flight_booking = flight_result
                    await session.commit()

            if research_output.selected_hotel is not None:
                from datetime import date as date_cls

                hotel_result = await propose_booking(
                    session,
                    ProposeHotelBookingInput(
                        trip_id=self.state.trip_id,
                        listing=research_output.selected_hotel,
                        check_in=date_cls.fromisoformat(self.state.depart_date),
                        check_out=(
                            date_cls.fromisoformat(self.state.return_date)
                            if self.state.return_date
                            else date_cls.fromisoformat(self.state.depart_date)
                        ),
                    ),
                )
                if isinstance(hotel_result, ToolError):
                    self.state.error = f"Hotel proposal failed: {hotel_result.message}"
                    await session.rollback()
                else:
                    self.state.hotel_booking = hotel_result
                    await session.commit()


def run_trip_planning_flow(
    *,
    origin_iata: str,
    destination_iata: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 1,
    max_budget_usd: float | None = None,
    requester_email: str | None = None,
) -> TripPlanningState:
    """Entrypoint: run `uv run python -m app.agents.flow` or import this
    directly. Dates are ISO strings ("2026-09-14") — see
    TripPlanningState's docstring for why."""
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
        }
    )
    return cast(TripPlanningState, flow.state)


if __name__ == "__main__":
    from datetime import date, timedelta

    final_state = run_trip_planning_flow(
        origin_iata="JFK",
        destination_iata="ATL",
        depart_date=(date.today() + timedelta(days=30)).isoformat(),
        adults=1,
        max_budget_usd=1500.0,
    )
    print("\nFinal state:", final_state.model_dump_json(indent=2))
