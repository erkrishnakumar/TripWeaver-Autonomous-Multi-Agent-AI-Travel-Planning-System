"""
Tests for app/agents/budget.py — pure Python, no LLM, no DB, no mocking
needed. Every case is a direct assertion on validate_budget()'s output.
"""

from __future__ import annotations

from app.agents.budget import validate_budget
from app.tools.schemas import CabinClass, FlightOffer, FlightSegment, HotelListing


def _flight(price: float) -> FlightOffer:
    return FlightOffer(
        offer_id="off_1",
        total_price_usd=price,
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


def _hotel(price: float) -> HotelListing:
    return HotelListing(
        search_result_id="srr_1",
        hotel_name="Test Hotel",
        latitude=1.0,
        longitude=1.0,
        estimated_price_total_usd=price,
        nights=3,
    )


class TestValidateBudget:
    def test_no_budget_set_is_always_within_budget(self):
        result = validate_budget(None, _flight(9999.0), _hotel(9999.0))
        assert result.within_budget is True
        assert result.max_budget_usd is None
        assert "No budget was set" in result.message

    def test_within_budget(self):
        result = validate_budget(1000.0, _flight(400.0), _hotel(300.0))
        assert result.within_budget is True
        assert result.total_cost_usd == 700.0
        assert "within the" in result.message

    def test_over_budget(self):
        result = validate_budget(500.0, _flight(400.0), _hotel(300.0))
        assert result.within_budget is False
        assert result.total_cost_usd == 700.0
        assert "over the" in result.message

    def test_exactly_at_budget_is_within_budget(self):
        result = validate_budget(700.0, _flight(400.0), _hotel(300.0))
        assert result.within_budget is True

    def test_no_flight_or_hotel_selected(self):
        result = validate_budget(500.0, None, None)
        assert result.total_cost_usd == 0.0
        assert result.within_budget is True

    def test_only_flight_selected(self):
        result = validate_budget(500.0, _flight(400.0), None)
        assert result.flight_cost_usd == 400.0
        assert result.hotel_cost_usd == 0.0
        assert result.total_cost_usd == 400.0
