# TripWeaver

Autonomous multi-agent travel planning system. CrewAI agents research
flights, hotels, car rentals, weather, and visa requirements, run a budget
check, and sequence a trip itinerary — but **a human must approve every
booking before anything is ever actually booked, and a real booking only
ever happens through one explicit, human-triggered endpoint.** No path in
this codebase lets an LLM decide when money moves; only plain, deterministic
Python ever writes a booking proposal or calls a real provider endpoint, and
the latter only after explicit human approval via the API.

## Architecture

```
tripweaver/
├── app/
│   ├── agents/          # CrewAI Researcher + Planner agents, budget check, Crews, and the
│   │                    #   Flow that enforces the human-approval gate (Phase 3)
│   ├── tools/           # Deterministic, typed, independently-tested tool functions (Phase 1)
│   │   ├── schemas.py           # Pydantic contracts shared by tools/agents/mcp_server/api
│   │   ├── propose_booking.py   # Gate 1: writes PENDING_APPROVAL, never books for real
│   │   ├── confirm_booking.py   # Gate 2: the ONLY code path allowed to call a real
│   │   │                        #   provider booking endpoint (flights/hotels; cars
│   │   │                        #   are not yet supported here — see docs below)
│   │   └── fixtures/            # Local JSON fixtures for USE_MOCK_DATA=true
│   ├── mcp_server/      # FastMCP server exposing the tool layer as MCP tools (Phase 2)
│   ├── api/             # FastAPI app: trip creation, status, Gate 1 (proceed) and
│   │                    #   Gate 2 (confirm/reject) endpoints (Phase 8, in progress)
│   ├── worker/          # Celery tasks (run_trip_planning, propose_trip_bookings) +
│   │                    #   Celery app config — runs the CrewAI Flow as a background
│   │                    #   job instead of blocking an HTTP request
│   ├── auth/            # Password hashing (bcrypt) — auth foundation, not yet wired
│   │                    #   into any endpoint (see docs/Auth_Requirement.md)
│   ├── db/              # SQLAlchemy models (Trip, Booking, Approval, ...) + async session
│   └── config.py        # Centralized settings (env vars) — see Local setup below
├── alembic/              # Alembic migration environment + versions
├── docs/                 # Design docs, roadmap, incident writeups (see Documentation below)
│                         #   NOTE: docs/ is intentionally excluded from git — see below
├── tests/                # Unit + integration tests (226 passing)
├── .github/workflows/    # CI pipeline (ruff, mypy, pytest)
├── .pre-commit-config.yaml  # Local pre-commit hooks (ruff check --fix + ruff format)
└── docker-compose.yml    # Local Postgres (Phase 5+) and Valkey (Celery broker/backend)
```

**A note on `docs/`**: this folder is deliberately excluded from git (see
`.gitignore`) — every design doc, incident writeup, and verification log in
it is tracked **locally only**, not pushed to GitHub. This is intentional,
not an oversight; if you're reading this from a fresh clone, `docs/` won't
be there.

## Local setup

1. Install [uv](https://docs.astral.sh/uv/) (fast Python package manager):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Create the virtual environment and install dependencies. The project uses
   optional dependency groups — `agents` (CrewAI), `mcp` (FastMCP), `api`
   (FastAPI/uvicorn/bcrypt), `worker` (Celery), and `dev` (pytest, ruff,
   mypy, pre-commit). Install everything to run the full stack locally:
   ```bash
   uv sync --extra dev --extra agents --extra mcp --extra api --extra worker
   ```
   CI installs the same set (`.github/workflows/ci.yml`) — a past incident
   where CI only installed `dev`/`agents`/`mcp` caused mypy to see `fastapi`
   as an untyped `Any` import and flag every FastAPI route decorator as
   broken, even though the code was correct. Keep CI's install list matching
   whatever extras the codebase actually imports.

3. Copy the env template and fill in your Duffel sandbox token:
   ```bash
   cp .env.example .env
   # edit .env and set DUFFEL_API_KEY=duffel_test_xxx
   ```
   Get a free sandbox token: sign up at https://app.duffel.com/join,
   go to Developers → Access tokens → New token (make sure you're in
   "Developer test mode"). Duffel Stays (hotels) and Cars need to be enabled
   separately on the same account.

   Other settings `app/config.py` reads from `.env` (see `.env.example` for
   every option and its default):
   - `USE_MOCK_DATA=true` — skip real network calls entirely; `search_flights()`,
     `search_hotels()`, and `search_car_rentals()` read local JSON fixtures
     instead. Use this for iteration/demos so you don't burn Duffel sandbox
     rate limits or depend on network access.
   - `LLM_PROVIDER` (`ollama` | `groq` | `gemini`) — the default backend the
     CrewAI researcher/planner agents reason with. Per-task-category
     overrides (`RESEARCH_FLIGHT_LLM_PROVIDER`, `RESEARCH_CAR_LLM_PROVIDER`,
     etc.) let you spread calls across multiple providers' free-tier quotas.
   - `OLLAMA_BASE_URL` / `OLLAMA_MODEL` — used when `LLM_PROVIDER=ollama`.
     Requires a running [Ollama](https://ollama.com/) server.
   - `GROQ_API_KEY` / `GEMINI_API_KEY` — used when `LLM_PROVIDER` selects
     that provider. `GROQ_API_KEY` is also used, independently of
     `LLM_PROVIDER`, by `check_visa_requirements()` — a deliberate, narrow
     exception to the local-first architecture.
   - `DATABASE_URL` — defaults to the Postgres service in
     `docker-compose.yml` (`localhost:5435`).
   - `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` — default to the Valkey
     service in `docker-compose.yml` (`localhost:6380`, DB `0`/`1`).
   - `JWT_SECRET_KEY` — **required** before `POST /auth/login` or any other
     endpoint (everything except `/health`, `/auth/register`, `/auth/login`)
     will work; the app refuses to sign/verify a token with an empty
     secret. Generate one with
     `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
     `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default `1440`, i.e. 24h) controls
     how long a token stays valid.
   - `RESEND_API_KEY` — optional; sends the real `POST /auth/forgot-password`
     email via [Resend](https://resend.com) (free tier available). Left
     empty, that endpoint falls back to returning the reset token directly
     in its response instead — fine for local dev, not how it's meant to
     work with this set. Resend's free tier (no verified domain) only
     delivers to the email address your Resend account itself is
     registered under.

4. Install the pre-commit hooks (runs `ruff check --fix` + `ruff format` on
   every commit):
   ```bash
   uv run pre-commit install
   ```

5. Start local Postgres and Valkey:
   ```bash
   docker-compose up -d
   ```

6. Apply database migrations:
   ```bash
   uv run alembic upgrade head
   ```

## Running things

Run any tool directly to smoke-test it end to end against the real APIs
(or fixtures, if `USE_MOCK_DATA=true`):
```bash
uv run python -m app.tools.flights
uv run python -m app.tools.hotels
uv run python -m app.tools.car_rentals
uv run python -m app.tools.weather
uv run python -m app.tools.ground_transport
```

Run the full CrewAI agent flow directly (research → budget check → plan →
draft trip → CLI approval prompt → propose bookings) — requires a running
Ollama server (or another configured `LLM_PROVIDER`):
```bash
uv run python -m app.agents.flow
```

Run the MCP server as a standalone process:
```bash
uv run python -m app.mcp_server.server
```

Run the full async stack (API + background worker) — this is the real,
non-CLI path: a trip's research/planning runs as a Celery background job,
and a human approves via HTTP rather than a blocking terminal prompt:
```bash
uv run uvicorn app.api.main:app --reload
uv run celery -A app.worker.celery_app worker --loglevel=info   # separate terminal
```
Then, register a user and log in — every endpoint below requires the
resulting bearer token (`Authorization: Bearer <access_token>`):
```bash
curl -X POST http://localhost:8000/auth/register -H "Content-Type: application/json" -d '{"email":"jane@example.com","password":"correcthorse"}'
curl -X POST http://localhost:8000/auth/login    -H "Content-Type: application/json" -d '{"email":"jane@example.com","password":"correcthorse"}'
# -> {"access_token": "...", "token_type": "bearer"}
```
```bash
AUTH='-H "Authorization: Bearer <access_token>"'
curl -X POST http://localhost:8000/trips $AUTH -H "Content-Type: application/json" -d '{...}'
# ... trip researches/plans in the background, lands in AWAITING_APPROVAL ...
curl -X POST http://localhost:8000/trips/{trip_id}/proceed $AUTH        # Gate 1: propose bookings
curl http://localhost:8000/trips/{trip_id}/bookings $AUTH               # find the approval_id(s) to act on
curl http://localhost:8000/trips/{trip_id}/audit-log $AUTH              # see everything that happened to this trip
curl -X POST http://localhost:8000/approvals/{approval_id}/confirm $AUTH -d '{...}'  # Gate 2: real booking
curl -X POST http://localhost:8000/approvals/{approval_id}/reject $AUTH -d '{...}'   # or reject
```
Every trip/approval endpoint also checks **ownership**, not just that a
token is valid — a trip or approval belonging to a different user (or with
no owner at all) 404s rather than 401s, so its existence is never confirmed
to someone who doesn't own it.

The Gate 1 → Gate 2 sequence itself is live-verified against the real
Duffel sandbox through the actual running API (not just as direct function
calls) — see `docs/Gate2_Live_Verification.md` §5.

**Windows note**: if you restart the Celery worker (e.g. to change env
vars), verify only one is left running with
`uv run celery -A app.worker.celery_app inspect ping` before creating a new
trip. `uv run celery ... --pool=solo` spawns a 3-process-deep tree; killing
only the top-level PID leaves the real worker running as an orphan that
keeps consuming tasks alongside the new one. Use `taskkill /PID <pid> /T /F`
(tree-kill) if you need to stop one. Full incident writeup in
`docs/Gate2_Live_Verification.md` §5.4.

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

`docs/` is tracked locally only (see the note above) — these files exist on
disk but are not in the GitHub repo:

- `docs/TripWeaver_Roadmap.md` — phase-by-phase project status, what's done, what's open
- `docs/Flight_And_Hotel_Real_Booking_Documentation.md` — live sandbox verification of `create_flight_order()`/`create_hotel_booking()`
- `docs/Car_Rentals_Documentation.md` — the Duffel Cars rollout: search/quote/book contract, real bugs found
- `docs/Car_Rental_Payment_Gap.md` — why car rental bookings can't be confirmed yet (Duffel requires a tokenized card; no frontend exists) and the full resolution plan
- `docs/Gate2_Live_Verification.md` — live end-to-end verification of `confirm_booking()`/`reject_booking()` against the real Duffel sandbox
- `docs/Multi_Car_Rental_Extension.md` — design for supporting more than one car rental per trip (not yet built)
- `docs/Auth_Requirement.md` — why authentication is a standing compliance requirement (Duffel's own service agreement), and what's built so far
- `docs/MCP_Server_Documentation.md` — MCP server design rationale
- `docs/TripWeaver_Two_Layer_Testing_and_CI.md` — the two-layer testing/CI system, how it's structured
- `docs/pytest_Documentation.md` — testing conventions used in this repo
- `docs/CI_Mypy_Ruff_Rationale.md` — why CI/mypy/ruff exist and what they catch
- `docs/Mypy_Documentation.md` — a real mypy/CI incident, root cause, and fix
- `app/agents/README.md` — CrewAI agent architecture, the human-approval-gate design, and testing approach
- `app/api/README.md` — FastAPI layer design notes

## Status

- [x] Phase 0: repo scaffold, schemas
- [x] Phase 1: tool layer (`search_flights`, `get_weather_forecast`, `search_hotels`, `search_car_rentals`, `check_visa_requirements`, `propose_booking`/`create_trip`)
- [x] Phase 2: MCP server wrapping (11 tools)
- [x] Phase 2.1: ground transport cost estimation
- [x] Phase 3: CrewAI agents + Flow with human-approval gate
- [x] Phase 4: Gate 1 + Gate 2 (`propose_booking()`/`confirm_booking()`/`reject_booking()`) — done, live-verified against the real Duffel sandbox **through the actual running HTTP API**, not just direct function calls (real flight order, reference `VFZC6E`)
- [x] Phase 5: Postgres persistence — verified live against real Postgres
- [🟡] Phase 6: observability — `GET /trips/{id}/bookings` and `GET /trips/{id}/audit-log` expose what was previously only visible via a raw DB query. Structured logging (`app/logging_config.py`, every line tagged `[trip=<uuid>]` across the API, worker, and CLI processes) is done and live-verified. Tracing and per-run cost tracking are still open.
- [x] Phase 7 (v1): agent evals — recorded real LLM/provider failure modes as deterministic regression tests. **Still open**: live-LLM-output-quality evals
- [x] Phase 8: API layer — `POST /trips`, `GET /trips/{id}`, `GET /trips/{id}/bookings`, Gate 1 (`/proceed`), and **Gate 2** (`POST /approvals/{id}/confirm|reject`) are all built and live-verified end to end against the real Duffel sandbox. Car rental confirmation is explicitly unsupported pending a card-tokenization frontend (see `docs/Car_Rental_Payment_Gap.md`). **Auth is now built and enforced**: `POST /auth/register`/`login` issue stateless JWT bearer tokens, `POST /auth/forgot-password`/`reset-password` support account recovery with real email delivery via Resend (falls back to returning the token directly only when `RESEND_API_KEY` isn't configured), and every trip/approval endpoint requires a token plus checks per-user ownership (see `docs/Auth_Requirement.md`).
- [ ] Phase 9: deployment

See `docs/TripWeaver_Roadmap.md` for the full breakdown, including known open items.
