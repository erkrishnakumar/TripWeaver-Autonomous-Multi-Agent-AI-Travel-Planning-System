# api/

Phase 8. Will contain the FastAPI app:
- POST /trips              — kick off a planning Flow
- GET  /trips/{id}         — status + itinerary
- POST /approvals/{id}/confirm | /reject   — the human approval gate
  (the ONLY code path allowed to call a real booking endpoint)

Not started yet — see README.md at repo root for phase checklist.
