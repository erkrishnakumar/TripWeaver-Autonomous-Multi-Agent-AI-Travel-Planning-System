"""
Tests for app/agents/tools.py — verifies the CrewAI @tool wrappers
correctly stringify success/error results, since CrewAI does NOT
auto-stringify tool return values (verified empirically before writing
these wrappers — see the module docstring in app/agents/tools.py).

Underlying app.tools.* functions are monkeypatched, same pattern as
tests/test_mcp_server.py — this file tests the wrapping contract, not the
already-covered Phase 1 tool logic.
"""

from __future__ import annotations

import json

import app.agents.tools as agent_tools
from app.tools.schemas import CabinClass, FlightOffer, FlightSegment, ToolError


def _flight_offer() -> FlightOffer:
    return FlightOffer(
        offer_id="off_1",
        total_price_usd=450.0,
        cabin_class=CabinClass.ECONOMY,
        stops_outbound=0,
        segments=[
            FlightSegment(
                carrier_iata="DL",
                carrier_name="Delta",
                flight_number="123",
                origin_iata="JFK",
                destination_iata="ATL",
                departs_at="2026-09-14T08:00:00",
                arrives_at="2026-09-14T10:30:00",
            )
        ],
    )


class TestSearchFlightsTool:
    def test_success_returns_valid_json_string(self, monkeypatch):
        from app.tools.schemas import FlightSearchResult

        fake_result = FlightSearchResult(
            query={
                "origin": "JFK",
                "destination": "ATL",
                "depart_date": "2026-09-14",
                "adults": 1,
            },
            offers=[_flight_offer()],
        )
        monkeypatch.setattr(agent_tools, "_search_flights", lambda query: fake_result)

        raw = agent_tools.search_flights_tool.run(
            query={
                "origin": "JFK",
                "destination": "ATL",
                "depart_date": "2026-09-14",
                "adults": 1,
            }
        )

        assert isinstance(raw, str)
        assert not raw.startswith("ERROR [")
        parsed = json.loads(raw)
        assert parsed["offers"][0]["offer_id"] == "off_1"

    def test_error_returns_prefixed_string_not_json(self, monkeypatch):
        monkeypatch.setattr(
            agent_tools,
            "_search_flights",
            lambda query: ToolError(
                tool_name="search_flights",
                error_type="config_error",
                message="DUFFEL_API_KEY is not set.",
                retryable=False,
            ),
        )

        raw = agent_tools.search_flights_tool.run(
            query={
                "origin": "JFK",
                "destination": "ATL",
                "depart_date": "2026-09-14",
                "adults": 1,
            }
        )

        assert raw == "ERROR [config_error]: DUFFEL_API_KEY is not set."


class TestEstimateGroundTransportTool:
    def test_success_returns_valid_json_string(self, monkeypatch):
        from app.tools.schemas import GroundTransportEstimateResult

        fake_result = GroundTransportEstimateResult(
            origin_resolved_name="Home",
            origin_latitude=1.0,
            origin_longitude=1.0,
            destination_resolved_name="Airport",
            destination_latitude=1.0,
            destination_longitude=1.0,
            distance_km=15.0,
            estimated_cost_usd_low=8.0,
            estimated_cost_usd_high=14.0,
        )
        monkeypatch.setattr(agent_tools, "_estimate_ground_transport", lambda query: fake_result)

        raw = agent_tools.estimate_ground_transport_tool.run(
            query={"origin_city": "Home", "destination_city": "Airport"}
        )

        parsed = json.loads(raw)
        assert parsed["distance_km"] == 15.0
        assert "NOT a real fare" in parsed["disclaimer"]


class TestAllResearchToolsList:
    def test_contains_exactly_five_tools(self):
        assert len(agent_tools.ALL_RESEARCH_TOOLS) == 5

    def test_no_booking_tools_present(self):
        names = {t.name for t in agent_tools.ALL_RESEARCH_TOOLS}
        assert "propose_flight_booking" not in names
        assert "propose_hotel_booking" not in names
        assert "create_trip" not in names
