# agents/

Phase 3. Will contain:
- `researcher.py` — CrewAI agent that calls search_flights/search_hotels/get_weather_forecast
- `budget.py` — validates options against constraints
- `planner.py` — sequences the itinerary
- `crew.py` — the Crew wiring the three agents together
- `flow.py` — the CrewAI Flow that wraps the Crew and enforces the human approval
  gate (propose → wait for human → confirm/reject)

Not started yet — see README.md at repo root for phase checklist.

# agents/

Phase 3 — done. CrewAI 1.15.16, LLM reasoning via local Ollama
(matching the project's local-first architecture; Groq stays the narrow,
explicit exception used only inside `check_visa_requirements`).

## Files

| File | What it is |
|---|---|
| `schemas.py` | `ResearchOutput`, `BudgetCheckResult`, `PlanOutput` — structured contracts between agent Tasks (separate from `app/tools/schemas.py`, which is the tool-layer contract) |
| `tools.py` | CrewAI `@tool` wrappers around the 5 read-only Phase 1 functions |
| `researcher.py` | `build_researcher_agent()` — has tool access, does the actual searching |
| `budget.py` | `validate_budget()` — plain Python, no LLM (see its own docstring for why) |
| `planner.py` | `build_planner_agent()` — no tools, reasons over what research+budget already found |
| `crew.py` | `build_research_crew()` / `build_planning_crew()` — two single-agent Crews, not one three-agent Crew (see below) |
| `flow.py` | `TripPlanningFlow` — sequences research → budget → plan → create_trip → **human approval** → propose bookings |

## Two deliberate departures from the original file-list description

**`crew.py` builds two Crews, not one three-agent Crew.** The README
originally described this as wiring "the three agents together"
(researcher, budget, planner). `budget.py` was always meant to be plain
Python, not an Agent — and critically, the budget check has to run
*between* research and planning (the planner needs to know whether the
researched options fit the budget). A single sequential Crew doesn't give
a verified, clean insertion point for external Python logic between two
agent Tasks. So `crew.py` builds `build_research_crew()` and
`build_planning_crew()` as two independent single-agent Crews, and
`flow.py` is what sequences them with `budget.validate_budget()` running
as ordinary Python in between.

**`create_trip`/`propose_flight_booking`/`propose_hotel_booking` are NOT
agent tools.** Only the 5 read-only tools are wrapped in `tools.py`. The
LLM agents never touch the database and never decide when a booking gets
proposed — only `flow.py` (plain Python) calls those functions, and only
after an explicit human approval step. This is a permanent architectural
boundary, not a gap to fill later: it's how this project's "mandatory
human-approval gate before anything is ever actually booked" principle
actually gets enforced at the code level, not just described in prose.

## The approval gate, concretely

```
@start   research()              → kicks off build_research_crew() (LLM + tools)
@listen  check_budget()          → plain Python: app.agents.budget.validate_budget()
@listen  plan()                  → kicks off build_planning_crew() (LLM, no tools)
@listen  persist_draft_trip()    → plain Python: app.tools.create_trip.create_trip()
@listen  wait_for_human_approval → *** CLI-ONLY DEMO PATH *** — blocking CLI input()
@listen  propose_bookings()      → plain Python: app.tools.propose_booking.propose_booking(),
                                    ONLY if approved == True
```

`wait_for_human_approval` was originally a Phase 8 placeholder for the real
approval mechanism; Phase 8 has since shipped (`POST /approvals/{id}/
confirm|reject`, see `app/api/main.py`), so this method now only matters
for the standalone CLI entrypoint (`uv run python -m app.agents.flow`) —
the real, production trigger is the API endpoint calling
`confirm_booking()`/`reject_booking()` directly (`app/tools/confirm_booking.py`),
never this method. Kept for local-dev/demo use, not deleted, since the
gate itself remains permanent regardless of which surface triggers it.

**Still never books anything for real, even after approval.**
`propose_bookings()` only ever calls `propose_booking()`, which writes
`PENDING_APPROVAL` rows — never a real provider booking endpoint. The
Flow's "approval" is approval to *propose* a booking for a second,
separate, human-triggered confirmation later (`POST /approvals/{id}/confirm`)
— not approval to actually book.

## A real CrewAI gotcha, caught and fixed here

`app/agents/tools.py` deliberately does **not** use
`from __future__ import annotations`, unlike every other file in this
project. With it present, CrewAI 1.15.16's `@tool` decorator generates a
perfectly valid-looking JSON schema at import time, but fails at actual
call time with `"<ToolName> is not fully defined ... call
<ToolName>.model_rebuild()"` — because the tool function's parameter
annotation becomes a lazy string under postponed evaluation, and CrewAI's
schema-validation step doesn't resolve it back to the real class. This
was caught by actually calling `.run()` on a wrapped tool in a test, not
by inspecting the generated schema (which shows nothing wrong). See
`tests/test_agent_tools.py`.

## Running it

```powershell
uv run python -m app.agents.flow
```

Requires a running local Ollama instance
(`OLLAMA_BASE_URL`/`OLLAMA_MODEL` in `.env`) — the researcher and planner
Agents both reason with it. The CLI will print the plan and prompt for
`y`/`N` at the approval step.

## Testing it

```powershell
uv run pytest tests/test_budget.py tests/test_agent_tools.py tests/test_flow.py -v
```

No real LLM or Ollama instance needed to run the tests: `Crew.kickoff()`
is monkeypatched with a fake object exposing `.pydantic` (matching
`CrewOutput`'s real, verified shape), and the human approval step's
`input()` is monkeypatched to simulate y/n. Everything else — real
`ResearchOutput`/`BudgetCheckResult`/`PlanOutput` construction, the real
budget-check arithmetic, real `create_trip()`/`propose_booking()` calls
against a real in-memory SQLite database, real approval-gate branching —
runs for real, same principle as `tests/test_mcp_server.py` and
`tests/test_propose_booking.py`.

## Not done in Phase 3 (tracked, not forgotten)

- ~~No real approval mechanism~~ — **shipped in Phase 8**, see the note
  above; only the CLI entrypoint still uses the blocking `input()` prompt.
- No retry/error-recovery strategy if `Crew.kickoff()` itself raises
  (e.g. Ollama unreachable) — currently an uncaught exception would
  propagate out of `flow.kickoff()`. Worth revisiting once Phase 6
  (observability) exists to actually see these failures in practice.
- CrewAI's built-in telemetry may print `"Failed to export span batch"`
  warnings in an offline/sandboxed environment — harmless, but consider
  `CREWAI_TRACING_ENABLED=false` in `.env` for a quieter local dev loop.
