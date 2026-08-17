# agents/

Phase 3. Will contain:
- `researcher.py` — CrewAI agent that calls search_flights/search_hotels/get_weather_forecast
- `budget.py` — validates options against constraints
- `planner.py` — sequences the itinerary
- `crew.py` — the Crew wiring the three agents together
- `flow.py` — the CrewAI Flow that wraps the Crew and enforces the human approval
  gate (propose → wait for human → confirm/reject)

Not started yet — see README.md at repo root for phase checklist.
