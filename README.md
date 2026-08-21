# TripWeaver

Autonomous multi-agent travel planning system. CrewAI agents research
flights, hotels, weather, and visa requirements, run a budget check, and
sequence a trip itinerary — but **a human must approve every booking before
anything is ever actually booked.** No path in this codebase lets an LLM
decide when money moves; only plain, deterministic Python ever writes a
booking proposal, and only after explicit human approval.

## Architecture

```
tripweaver/
├── app/
│   ├── agents/          # CrewAI Researcher + Planner agents, budget check, Crews, and the
│   │                    #   Flow that enforces the human-approval gate (Phase 3)
│   ├── tools/           # Deterministic, typed, independently-tested tool functions (Phase 1)
│   │   ├── schemas.py   # Pydantic contracts shared by tools/agents/mcp_server/api
│   │   └── fixtures/    # Local JSON fixtures for USE_MOCK_DATA=true
│   ├── mcp_server/      # FastMCP server exposing the tool layer as MCP tools (Phase 2)
│   ├── api/             # FastAPI app: trip creation, status, approval gate — not built yet (Phase 8)
│   ├── db/               # SQLAlchemy models (Trip, Booking, Approval, ...) + session
│   └── config.py          # Centralized settings (env vars) — see Local setup below
├── alembic/              # Alembic migration environment + versions
├── docs/                 # Design docs, roadmap, incident writeups (see Documentation below)
├── tests/                # Unit + integration tests (106 passing)
├── .github/workflows/    # CI pipeline (ruff, mypy, pytest)
├── .pre-commit-config.yaml  # Local pre-commit hooks (ruff check --fix + ruff format)
└── docker-compose.yml    # Local Postgres for Phase 5+
```

## Local setup

1. Install [uv](https://docs.astral.sh/uv/) (fast Python package manager):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Create the virtual environment and install dependencies. The project uses
   optional dependency groups — `agents` (CrewAI), `mcp` (FastMCP), `api`
   (FastAPI, not yet used), and `dev` (pytest, ruff, mypy, pre-commit).
   Install everything you'll actually run locally:
   ```bash
   uv sync --extra dev --extra agents --extra mcp
   ```

3. Copy the env template and fill in your Duffel sandbox token:
   ```bash
   cp .env.example .env
   # edit .env and set DUFFEL_API_KEY=duffel_test_xxx
   ```
   Get a free sandbox token: sign up at https://app.duffel.com/join,
   go to Developers → Access tokens → New token (make sure you're in
   "Developer test mode"). Duffel Stays (hotel search) needs to be enabled
   separately on the same account.

   Other settings `app/config.py` reads from `.env` (see that file for every
   option and its default):
   - `USE_MOCK_DATA=true` — skip real network calls entirely; `search_flights()`
     and `search_hotels()` read local JSON fixtures instead. Use this for
     iteration/demos so you don't burn Duffel sandbox rate limits or depend
     on network access.
   - `OLLAMA_BASE_URL` / `OLLAMA_MODEL` — the local-first LLM the CrewAI
     agents (researcher, planner) reason with. Requires a running
     [Ollama](https://ollama.com/) server.
   - `GROQ_API_KEY` — only used by `check_visa_requirements()`, a deliberate,
     narrow exception to the local-first architecture.
   - `DATABASE_URL` — defaults to the Postgres service in `docker-compose.yml`
     (`localhost:5435`).

4. Install the pre-commit hooks (runs `ruff check --fix` + `ruff format` on
   every commit):
   ```bash
   uv run pre-commit install
   ```

5. (Optional) Start local Postgres:
   ```bash
   docker-compose up -d
   ```

## Running things

Run any tool directly to smoke-test it end to end against the real APIs
(or fixtures, if `USE_MOCK_DATA=true`):
```bash
uv run python -m app.tools.flights
uv run python -m app.tools.hotels
uv run python -m app.tools.weather
uv run python -m app.tools.ground_transport
```

Run the full CrewAI agent flow (research → budget check → plan → draft trip
→ human approval prompt → propose bookings) — requires a running Ollama
server:
```bash
uv run python -m app.agents.flow
```

Run the MCP server as a standalone process:
```bash
uv run python -m app.mcp_server.server
```

## Tests

```bash
# Run one test file while iterating on a feature
uv run pytest tests/test_flights.py -v

# Run the full suite
uv run pytest tests -v
```

## Code quality

Three checks run both locally (pre-commit, on every commit) and in CI (on
every push/PR to `main`):

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy app/              # strict-mode static type checking
```

See `docs/CI_Mypy_Ruff_Rationale.md` for why each of these matters for this
project specifically, and `docs/Mypy_Documentation.md` for a full writeup of
a real mypy/CI incident and fix.

## Documentation

- `docs/TripWeaver_Roadmap.md` — phase-by-phase project status, what's done, what's open
- `docs/MCP_Server_Documentation.md` — MCP server design rationale
- `docs/pytest_Documentation.md` — testing conventions used in this repo
- `docs/CI_Mypy_Ruff_Rationale.md` — why CI/mypy/ruff exist and what they catch
- `docs/Mypy_Documentation.md` — a real mypy/CI incident, root cause, and fix
- `app/agents/README.md` — CrewAI agent architecture, the human-approval-gate design, and testing approach

## Status

- [x] Phase 0: repo scaffold, schemas
- [x] Phase 1: tool layer (`search_flights`, `get_weather_forecast`, `search_hotels`, `check_visa_requirements`, `propose_booking`/`create_trip`)
- [x] Phase 2: MCP server wrapping
- [x] Phase 2.1: ground transport cost estimation
- [x] Phase 3: CrewAI agents + Flow with human-approval gate
- [ ] Phase 4: real confirm/reject API endpoint (propose_booking() itself is done; see Phase 8)
- [x] Phase 5: Postgres persistence — models done and tested; real Postgres round-trip not yet verified
- [ ] Phase 6: observability
- [ ] Phase 7: CI evals
- [ ] Phase 8: API layer
- [ ] Phase 9: deployment

See `docs/TripWeaver_Roadmap.md` for the full breakdown, including known open items.
