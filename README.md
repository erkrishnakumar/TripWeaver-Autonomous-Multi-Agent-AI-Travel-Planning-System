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
│   │   │                        #   provider booking endpoint (flights, hotels, and
│   │   │                        #   cars — the last needs a card payment token)
│   │   └── fixtures/            # Local JSON fixtures for USE_MOCK_DATA=true
│   ├── mcp_server/      # FastMCP server exposing the tool layer as MCP tools (Phase 2).
│   │                    #   Every tool requires the same JWT bearer token the API issues
│   ├── api/             # FastAPI app: auth, trip creation, status, Gate 1 (proceed) and
│   │                    #   Gate 2 (confirm/reject) endpoints (Phase 8)
│   ├── worker/          # Celery tasks (run_trip_planning, propose_trip_bookings) +
│   │                    #   Celery app config — runs the CrewAI Flow as a background
│   │                    #   job instead of blocking an HTTP request
│   ├── auth/            # JWT issuance/verification + bcrypt password hashing +
│   │                    #   single-use reset tokens (see docs/Auth_Requirement.md)
│   ├── email/           # Real email delivery via Resend: password reset + booking-confirmed
│   ├── db/              # SQLAlchemy models (Trip, Booking, Approval, ...) + async session
│   ├── logging_config.py # One shared structured-logging setup for API/worker/CLI
│   └── config.py        # Centralized settings (env vars) — see Local setup below
├── frontend/             # React 19 + Vite + TypeScript + Tailwind SPA: auth, trip
│                         #   creation/detail, approval dialogs, Duffel card form (Phase 10)
├── alembic/              # Alembic migration environment + versions
├── docs/                 # Design docs, roadmap, incident writeups (see Documentation below)
│                         #   NOTE: docs/ is intentionally excluded from git — see below
├── tests/                # Unit + integration tests (253 passing)
├── .github/workflows/    # CI: backend (ruff, mypy, pytest) + frontend (oxlint, tsc, build)
├── .pre-commit-config.yaml  # Local pre-commit hooks (ruff check --fix + ruff format)
├── Dockerfile             # One image, two roles (api/worker) -- see docker-compose.yml
├── docker-compose.yml    # Full stack: postgres, valkey, migrate (one-off), api, worker
└── docker-compose.prod.yml  # Production overlay (see Deployment below)
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
     `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default `30`) controls how long a
     token stays valid — deliberately short, since there's no refresh-token
     flow and the frontend keeps the token in memory only (never
     `localStorage`), so a page reload requires logging in again rather than
     leaving a bearer token somewhere an injected script could read it.
   - `RESEND_API_KEY` — optional; sends two real emails via
     [Resend](https://resend.com) (free tier available): `POST
     /auth/forgot-password`'s reset link, and a booking-confirmed email
     (traveler's own address — passenger email for flights, `contact_email`
     for hotels, driver email for cars) the moment `POST
     /approvals/{id}/confirm` actually books something with the provider.
     Left empty, forgot-password falls back to returning the reset token
     directly in its response instead (fine for local dev); the booking
     confirmation email is just skipped entirely, since a real booking must
     never be reported as failed over a missing/failed notification. Resend's
     free tier (no verified domain) only delivers to the email address your
     Resend account itself is registered under.

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
curl http://localhost:8000/trips/{trip_id}/bookings/{booking_id}/confirm-info $AUTH  # real ids needed to confirm (e.g. passenger_ids)
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

Run the frontend (expects the API on `http://localhost:8000`, overridable
via `VITE_API_BASE_URL` in `frontend/.env.local`):
```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```
The API's `CORS_ALLOWED_ORIGINS` already includes the Vite dev server's
default origin. Because the frontend holds its bearer token in memory only,
a hard reload always sends you back to the login page — that's deliberate,
not a bug.

**Windows note**: if you restart the Celery worker (e.g. to change env
vars), verify only one is left running with
`uv run celery -A app.worker.celery_app inspect ping` before creating a new
trip. `uv run celery ... --pool=solo` spawns a 3-process-deep tree; killing
only the top-level PID leaves the real worker running as an orphan that
keeps consuming tasks alongside the new one. Use `taskkill /PID <pid> /T /F`
(tree-kill) if you need to stop one. Full incident writeup in
`docs/Gate2_Live_Verification.md` §5.4.

## Deployment

The full stack runs in Docker -- not just Postgres/Valkey, but the API and
Celery worker too, built from one shared `Dockerfile` (both services run
the same image; only the command differs).

```bash
docker compose build
docker compose up -d
```

This starts `postgres`, `valkey`, a one-off `migrate` service (runs
`alembic upgrade head` and exits -- `api`/`worker` both wait for it to
finish before starting), `api` (port `8000`), and `worker`. Requires a
real `.env` in the project root (`docker-compose.yml`'s `env_file: .env`) --
copy `.env.example` first if you haven't. Inside the compose network,
`DATABASE_URL`/`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`/
`RATE_LIMIT_STORAGE_URL` are overridden to address `postgres`/`valkey` by
their service name on the internal network, not the `localhost:5435`/
`:6380` your `.env` uses for host-side tools like `uv run alembic`.

Check it's up:
```bash
curl http://localhost:8000/health
docker compose logs -f api      # or worker
```

For a production-like run (no host-published DB/Valkey ports, `restart:
always` instead of `unless-stopped`), layer the prod overlay on top:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

**Rate limiting** is active on every request (`app/api/rate_limit.py`,
`slowapi` backed by Valkey): a `60/minute` default, `10/minute` on
register/login/reset-password, and a tighter `5/hour` on
forgot-password specifically (each call there triggers a real, metered
email send). All of this is live-verified against real containers, not
just built -- see `docs/TripWeaver_Roadmap.md`'s Phase 9 section for the
full writeup, including a real bug (`RATE_LIMIT_STORAGE_URL` missing from
the compose file's environment overrides) found by actually running the
stack rather than just building it.

## Tests

```bash
# Run one test file while iterating on a feature
uv run pytest tests/test_flights.py -v

# Run the full suite
uv run pytest tests -v
```

## Code quality

Three backend checks run both locally (pre-commit, on every commit) and in
CI (on every push/PR to `main`):

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy app/              # strict-mode static type checking
```

CI runs the frontend's own checks as a second, parallel job:

```bash
cd frontend
npm run lint    # oxlint
npm run build   # tsc -b (type check) + vite build
```

See `docs/CI_Mypy_Ruff_Rationale.md` for why each of these matters for this
project specifically, and `docs/Mypy_Documentation.md` for a full writeup of
a real mypy/CI incident and fix.

## Documentation

`docs/` is tracked locally only (see the note above) — these files exist on
disk but are not in the GitHub repo:

- `docs/TripWeaver_Roadmap.md` — phase-by-phase project status, what's done, what's open
- `docs/TripWeaver_Technical_Deep_Dive.md` — a full technical walkthrough of the whole system (architecture, request lifecycle, every layer, engineering decisions, real problems hit and how they were solved), written for explaining the project end to end
- `docs/TripWeaver_Project_Scope.md` — what the system does and doesn't do, with everything labelled implemented / partially implemented / out of scope / future
- `docs/Flight_And_Hotel_Real_Booking_Documentation.md` — live sandbox verification of `create_flight_order()`/`create_hotel_booking()`
- `docs/Car_Rentals_Documentation.md` — the Duffel Cars rollout: search/quote/book contract, real bugs found
- `docs/Car_Rental_Payment_Gap.md` — why car rental bookings originally couldn't be confirmed (Duffel Cars requires a tokenized card, unlike Flights/Stays) and §5's writeup of how the client-side card-tokenization flow resolved it
- `docs/Gate2_Live_Verification.md` — live end-to-end verification of `confirm_booking()`/`reject_booking()` against the real Duffel sandbox
- `docs/Multi_Car_Rental_Extension.md` — the design for supporting more than one car rental per trip (now built)
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
- [x] Phase 2: MCP server wrapping (11 tools) — every tool now requires the same JWT bearer token the HTTP API issues, and MCP-created trips are owned by the authenticated caller
- [x] Phase 2.1: ground transport cost estimation
- [x] Phase 3: CrewAI agents + Flow with human-approval gate
- [x] Phase 4: Gate 1 + Gate 2 (`propose_booking()`/`confirm_booking()`/`reject_booking()`) — done, live-verified against the real Duffel sandbox **through the actual running HTTP API**, not just direct function calls (real flight order, reference `VFZC6E`). Hardened further after a live run: the formatter agent is instructed to omit `selected_hotel` entirely when it never got a real `search_result_id`, but a model doesn't always follow that faithfully — it can report the object anyway with a blank id. `flow.py`'s propose step now checks for that explicitly instead of trusting the prompt alone, so a blank id is skipped with a clear audit-log error rather than burning a doomed provider API call.
- [x] Phase 5: Postgres persistence — verified live against real Postgres
- [🟡] Phase 6: observability — `GET /trips/{id}/bookings` and `GET /trips/{id}/audit-log` expose what was previously only visible via a raw DB query. Structured logging (`app/logging_config.py`, every line tagged `[trip=<uuid>]` across the API, worker, and CLI processes) is done and live-verified. Tracing and per-run cost tracking are still open.
- [x] Phase 7 (v1): agent evals — recorded real LLM/provider failure modes as deterministic regression tests. **Still open**: live-LLM-output-quality evals
- [x] Phase 8: API layer — `POST /trips`, `GET /trips/{id}`, `GET /trips/{id}/bookings`, Gate 1 (`/proceed`), and **Gate 2** (`POST /approvals/{id}/confirm|reject`) are all built and live-verified end to end against the real Duffel sandbox. Car rental confirmation now works too, via the frontend's Duffel card-tokenization flow (see `docs/Car_Rental_Payment_Gap.md` §5). A real Gate 2 confirmation now also sends the traveler a booking-confirmed email via Resend (best-effort — a notification failure never turns a real, already-committed booking into a reported failure). **Auth is now built and enforced**: `POST /auth/register`/`login` issue stateless JWT bearer tokens (`GET`/`PATCH /auth/me` for reading and updating the caller's own profile — currently just a display name), `POST /auth/forgot-password`/`reset-password` support account recovery with real email delivery via Resend (falls back to returning the token directly only when `RESEND_API_KEY` isn't configured), and every trip/approval endpoint requires a token plus checks per-user ownership (see `docs/Auth_Requirement.md`).
- [x] Phase 9: deployment — the full stack (API, worker, Postgres, Valkey) runs in Docker via `docker-compose.yml`, with a `docker-compose.prod.yml` overlay and Valkey-backed rate limiting (`slowapi`). Live-verified with real containers, not just built. Deliberately out of scope for now: multi-replica scaling, a real secrets manager, TLS termination.
- [🟡] Phase 10: frontend — React 19 + Vite + TypeScript + Tailwind SPA under `frontend/`: login/register (with live password-strength feedback and a show/hide toggle) /forgot-reset-password, a profile menu backed by `/auth/me` that supports editing your display name, trip creation, and trip detail with a **live per-stage research progress tracker** (flight → hotel → car rental → context → format, driven by real `research.{stage}_completed` audit events emitted from a CrewAI `task_callback` — not a simulated progress bar) plus the audit-log activity feed and per-booking-type approval dialogs including the Duffel card form for car rentals. A local, per-browser "recent trips" shortcut list (there's no `GET /trips` list endpoint yet) supports removing entries. Gate 1 approval shows an immediate "verifying and proposing" state and keeps polling through the `awaiting_approval → approved/failed` window instead of appearing to hang while `propose_trip_bookings` runs in the background. Token is held **in memory only** (never `localStorage`) as deliberate XSS hardening — a full page reload always requires logging in again. Still open: the card form's actual card-entry step hasn't been manually verified in a real browser (an automated browser got a blank Duffel card-vault iframe, most likely that hosted page's own anti-automation behavior).

See `docs/TripWeaver_Roadmap.md` for the full breakdown, including known open items.

Two documents aimed at explaining this project rather than building it:
`docs/TripWeaver_Technical_Deep_Dive.md` (a full technical walkthrough, for
interviews) and `docs/TripWeaver_Project_Scope.md` (what is and isn't in
scope, and what's genuinely future work).
