# TripWeaver

Autonomous multi-agent travel planning system. Agents research, budget-check,
and sequence a trip itinerary; a human approves every booking before money moves.

## Architecture

```
tripweaver/
├── agents/            # CrewAI agent + crew + flow definitions
├── tools/             # Deterministic, typed, unit-tested tool functions
│   └── schemas.py     # Pydantic contracts shared by tools/agents/api
├── mcp_server/         # FastMCP server exposing tools/ as MCP tools
├── api/                # FastAPI app: trip creation, status, approval gate
├── db/                 # SQLAlchemy models + Alembic migrations
├── infra/              # docker-compose, Dockerfiles
├── tests/               # unit + integration tests
└── .github/workflows/   # CI pipeline
```

## Local setup (Phase 0/1)

1. Install [uv](https://docs.astral.sh/uv/) (fast Python package manager):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Create the virtual environment and install dependencies:
   ```bash
   uv sync
   ```
3. Copy the env template and fill in your Duffel sandbox token:
   ```bash
   cp .env.example .env
   # edit .env and set DUFFEL_API_KEY=duffel_test_xxx
   ```
   Get a free sandbox token: sign up at https://app.duffel.com/join,
   go to Developers → Access tokens → New token (make sure you're in
   "Developer test mode").
4. Run the tool directly to confirm it works end to end:
   ```bash
   uv run python -m tools.flights
   ```
5. Run the test suite:
   ```bash
   uv run pytest -v
   ```

## Status

- [x] Phase 0: repo scaffold, schemas
- [x] Phase 1: `search_flights()` tool against Duffel sandbox
- [ ] Phase 2: MCP server wrapping
- [ ] Phase 3: CrewAI agents/flow
- [ ] Phase 4: approval gate + booking execution
- [ ] Phase 5: Postgres persistence
- [ ] Phase 6: observability
- [ ] Phase 7: CI evals
- [ ] Phase 8: API layer
- [ ] Phase 9: deployment




## To test the weather.py and flights.py
uv run python -m pytest tests/test_weather.py tests/test_flights.py -v

## To run all the tests present in all the test files
uv run python -m pytest tests -v

## if one package needs to be installed
uv add groq