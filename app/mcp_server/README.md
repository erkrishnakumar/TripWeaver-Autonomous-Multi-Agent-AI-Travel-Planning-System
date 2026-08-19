# mcp_server/

Phase 2. Will use FastMCP to expose the functions in tools/ (search_flights,
search_hotels, get_weather_forecast, check_visa_requirements, propose_booking)
as MCP tools, runnable as a standalone process separate from the agent runtime.

Not started yet — see README.md at repo root for phase checklist.

# mcp_server/

Phase 2 — done. Wraps the tool layer as a FastMCP (v3.4.7) server, runnable
as a standalone process separate from the agent runtime.

## Tools exposed (8, not 5)

| Tool | Wraps | Backing |
|---|---|---|
| `search_flights` | `app.tools.flights.search_flights` | Duffel Flights (or fixtures) |
| `get_weather_forecast` | `app.tools.weather.get_weather_forecast` | Open-Meteo |
| `search_hotels` | `app.tools.hotels.search_hotels` | Duffel Stays (or fixtures) |
| `check_visa_requirements` | `app.tools.visa.check_visa_requirements` | Groq (or fixtures) |
| `estimate_ground_transport` | `app.tools.ground_transport.estimate_ground_transport` | None — local math only |
| `create_trip` | `app.tools.create_trip.create_trip` | Postgres |
| `propose_flight_booking` | `app.tools.propose_booking.propose_booking` | Postgres |
| `propose_hotel_booking` | `app.tools.propose_booking.propose_booking` | Postgres |

Three deliberate departures from the original "five tools" framing:

- **`create_trip` is exposed.** An MCP client needs a `trip_id` before it
  can call either `propose_*_booking` tool — without `create_trip` this
  server isn't usable end-to-end for its actual purpose.
- **`propose_booking()` is exposed as two tools, not one.** Its Python
  signature takes `Union[ProposeFlightBookingInput, ProposeHotelBookingInput]`,
  which doesn't map cleanly onto a single MCP tool's JSON schema — a
  calling agent would see one ambiguous input instead of two clearly-typed
  tools. `propose_flight_booking` / `propose_hotel_booking` mirror the two
  `*BookingInput` models that already exist in `schemas.py`.
- **`estimate_ground_transport` was added later (Phase 2.1)**, for a
  different reason than the other two — see below.

## Why `estimate_ground_transport` exists, and why it's not a real booking

TripWeaver does **not** integrate a ride-hailing API for legs like
home→airport or airport→hotel. Two real options were evaluated and
rejected before building this:

- **Duffel Cars** — a real Duffel product (launched April 2026) covering
  Avis/Hertz/Sixt/Enterprise/Europcar. But it's **self-drive car rental**
  (you take the keys and drive yourself), not a chauffeured transfer.
  Using it for a one-off airport hop is technically possible (Duffel Cars
  supports one-way rentals with different pickup/drop-off locations) but
  practically wrong — nobody rents a car just to get dropped at a rental
  counter.
- **Real ride-hailing APIs** — Ola and Rapido have no public booking API
  at all. Uber's requires an enterprise partnership (Uber for Business /
  Guest Rides), not a self-serve sandbox token like Duffel's.

Since neither fits, `estimate_ground_transport` gives a **rough, clearly
disclaimed cost estimate** instead — same principle as
`check_visa_requirements()` returning an informational-only answer where
no authoritative source exists, rather than faking precision it doesn't
have. It:

- Reuses `geocode_city()` — the same geocoding path `weather.py` and
  `hotels.py` already use — for any city-name input.
- Computes straight-line (haversine) distance with a rough road-distance
  correction factor — no routing API, since this is explicitly an
  estimate, not a quote.
- Returns a **range** (`estimated_cost_usd_low`/`estimated_cost_usd_high`),
  not a single falsely-precise number.
- Has **no approval-gate counterpart** — unlike every other stateful tool
  here, there's nothing to approve, because nothing is ever bookable. No
  DB writes, no new `BookingType`.
- Is the only tool in this server with **zero external API dependency** —
  no Duffel, no Groq, no new API key required.

## Error handling

Every `app.tools.*` function returns `Result | ToolError` (a Pydantic
model) rather than raising. FastMCP has its own, differently-named
`ToolError` (`fastmcp.exceptions.ToolError`) — an *exception*, and the
framework's sanctioned way to surface a clear failure to the calling
agent/LLM. `server.py`'s `_unwrap()` helper translates one into the other.

**Gotcha found and regression-tested while building this**: a domain
`ToolError` returned *without* going through `_unwrap()` doesn't surface
its message — FastMCP tries to validate the raw `ToolError` object against
the tool's declared success return type, fails, and raises its own generic
`"Output validation error"` instead, discarding the real reason. Every
tool function in `server.py` routes its return value through `_unwrap()`
with no exceptions; see the module docstring there and
`tests/test_mcp_server.py`'s regression coverage.

## DB session lifecycle

`create_trip`, `propose_flight_booking`, and `propose_hotel_booking` each
open their own `async with get_session()` block per call and commit within
that same call — session-per-call, not a session held open across this
(long-running) server process's lifetime. `estimate_ground_transport`
opens no session at all — it never touches the database.

## Running it

```powershell
uv run python -m app.mcp_server.server
```

## Testing it

```powershell
uv run pytest tests/test_mcp_server.py -v
```

Tests go through `fastmcp.Client` against the real `mcp` server object (an
in-memory transport), not by calling the decorated functions directly, so
they verify the actual MCP-level argument schema and error surface an
agent would see. DB-backed tools are tested against a real in-memory
SQLite database per test, same convention as
`tests/test_propose_booking.py`.

## Not done in Phase 2 (tracked for later phases)

- No auth/transport hardening — `mcp.run()` uses FastMCP's default
  (stdio) transport. HTTP transport + auth, if ever needed, is a Phase 6/9
  concern, not Phase 2.
- No agent actually calls this server yet — that's Phase 3.