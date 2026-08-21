"""
Tests for app/mcp_server/server.py.

Two testing strategies, matching the project's existing conventions:

- search_flights / get_weather_forecast / search_hotels /
  check_visa_requirements each wrap an already-tested (Phase 1, 61 passing
  tests) deterministic tool function. These tests monkeypatch the
  underlying tool function so they isolate exactly what Phase 2 adds: MCP
  registration, argument (de)serialization, and translating a domain
  ToolError into a real MCP-level tool-call error. Re-testing Duffel/
  Open-Meteo/Groq parsing here would just duplicate Phase 1's coverage.

- create_trip / propose_flight_booking / propose_hotel_booking are
  DB-backed. Like tests/test_propose_booking.py, these run against a real
  in-memory SQLite database per test rather than a mocked session, because
  their entire job is correct persistence and idempotency — the same
  rationale test_propose_booking.py already documents.

Every test goes through fastmcp.Client against the real `mcp` server
object (in-memory transport), not by calling the decorated functions
directly, so these tests also verify the actual MCP-level argument
schema and error surface an agent/LLM client would see — not just that
the underlying Python function works.

REGRESSION TEST OF NOTE: test_domain_tool_error_message_survives_unwrap
below exists because of a real bug caught while building this file: a
domain ToolError returned WITHOUT going through _unwrap() gets silently
replaced by FastMCP's own generic "Output validation error" message,
discarding the real reason. This test would fail if that discipline is
ever violated in server.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError as MCPToolError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.mcp_server.server as server_module
from app.db.base import Base
from app.tools.schemas import (
    CabinClass,
    DailyForecast,
    FlightOffer,
    FlightSearchResult,
    FlightSegment,
    HotelListing,
    HotelSearchResult,
    TravelPurpose,
    VisaCheckResult,
    WeatherForecastResult,
)
from app.tools.schemas import ToolError as DomainToolError

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sqlite_session_override(monkeypatch):
    """Point the server's get_session() at a fresh in-memory SQLite database
    for the duration of one test — same fixture pattern as
    tests/test_propose_booking.py, applied at the MCP-server layer."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(server_module, "get_session", _get_session)
    yield
    await engine.dispose()


@pytest.fixture
def flight_offer() -> FlightOffer:
    return FlightOffer(
        offer_id="off_test_001",
        total_price_usd=450.00,
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


@pytest.fixture
def hotel_listing() -> HotelListing:
    return HotelListing(
        search_result_id="srr_test_001",
        hotel_name="Test Hotel",
        hotel_rating=4.0,
        latitude=13.75,
        longitude=100.5,
        estimated_price_total_usd=600.0,
        nights=3,
    )


def _depart_date_str() -> str:
    return (date.today() + timedelta(days=30)).isoformat()


async def _mcp_create_trip(client: Client, **overrides) -> object:
    args = {
        "origin_iata": "jfk",
        "destination_iata": "atl",
        "depart_date": _depart_date_str(),
        "adults": 1,
    }
    args.update(overrides)
    result = await client.call_tool("create_trip", args)
    return result.data


# ---------------------------------------------------------------------------
# search_flights
# ---------------------------------------------------------------------------


class TestSearchFlightsTool:
    async def test_success_returns_offers(self, monkeypatch, flight_offer):
        fake_result = FlightSearchResult(
            query={
                "origin": "JFK",
                "destination": "ATL",
                "depart_date": _depart_date_str(),
                "adults": 1,
            },
            offers=[flight_offer],
            is_mock=True,
        )
        monkeypatch.setattr(server_module, "_search_flights", lambda query: fake_result)

        async with Client(server_module.mcp) as client:
            result = await client.call_tool(
                "search_flights",
                {
                    "query": {
                        "origin": "JFK",
                        "destination": "ATL",
                        "depart_date": _depart_date_str(),
                        "adults": 1,
                    }
                },
            )

        assert result.data.is_mock is True
        assert result.data.offers[0].offer_id == "off_test_001"
        assert result.data.offers[0].cabin_class == "economy"

    async def test_tool_error_is_raised_with_real_message(self, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "_search_flights",
            lambda query: DomainToolError(
                tool_name="search_flights",
                error_type="config_error",
                message="DUFFEL_API_KEY is not set.",
                retryable=False,
            ),
        )

        async with Client(server_module.mcp) as client:
            with pytest.raises(MCPToolError, match="config_error.*DUFFEL_API_KEY"):
                await client.call_tool(
                    "search_flights",
                    {
                        "query": {
                            "origin": "JFK",
                            "destination": "ATL",
                            "depart_date": _depart_date_str(),
                            "adults": 1,
                        }
                    },
                )


# ---------------------------------------------------------------------------
# get_weather_forecast
# ---------------------------------------------------------------------------


class TestGetWeatherForecastTool:
    async def test_success_returns_forecast(self, monkeypatch):
        fake_result = WeatherForecastResult(
            resolved_location_name="Atlanta, US",
            latitude=33.75,
            longitude=-84.39,
            daily=[
                DailyForecast(
                    date=date.today() + timedelta(days=2),
                    temp_max_c=28.0,
                    temp_min_c=18.0,
                    precipitation_probability_pct=10,
                    weather_code=1,
                    weather_description="Mainly clear",
                )
            ],
        )
        monkeypatch.setattr(server_module, "_get_weather_forecast", lambda query: fake_result)

        async with Client(server_module.mcp) as client:
            result = await client.call_tool(
                "get_weather_forecast",
                {
                    "query": {
                        "city": "Atlanta",
                        "start_date": (date.today() + timedelta(days=2)).isoformat(),
                        "end_date": (date.today() + timedelta(days=2)).isoformat(),
                    }
                },
            )

        assert result.data.resolved_location_name == "Atlanta, US"
        assert result.data.daily[0].weather_description == "Mainly clear"

    async def test_forecast_range_exceeded_is_raised_with_real_message(self, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "_get_weather_forecast",
            lambda query: DomainToolError(
                tool_name="get_weather_forecast",
                error_type="forecast_range_exceeded",
                message="Weather forecasts are only available up to about 15 days out.",
                retryable=False,
            ),
        )

        async with Client(server_module.mcp) as client:
            with pytest.raises(MCPToolError, match="forecast_range_exceeded"):
                await client.call_tool(
                    "get_weather_forecast",
                    {
                        "query": {
                            "city": "Atlanta",
                            "start_date": (date.today() + timedelta(days=60)).isoformat(),
                            "end_date": (date.today() + timedelta(days=62)).isoformat(),
                        }
                    },
                )


# ---------------------------------------------------------------------------
# search_hotels
# ---------------------------------------------------------------------------


class TestSearchHotelsTool:
    async def test_success_returns_listings(self, monkeypatch, hotel_listing):
        fake_result = HotelSearchResult(
            query={
                "city": "Atlanta",
                "check_in": _depart_date_str(),
                "check_out": (date.today() + timedelta(days=33)).isoformat(),
                "adults": 2,
            },
            resolved_location_name="Atlanta, US",
            listings=[hotel_listing],
            is_mock=True,
        )
        monkeypatch.setattr(server_module, "_search_hotels", lambda query: fake_result)

        async with Client(server_module.mcp) as client:
            result = await client.call_tool(
                "search_hotels",
                {
                    "query": {
                        "city": "Atlanta",
                        "check_in": _depart_date_str(),
                        "check_out": (date.today() + timedelta(days=33)).isoformat(),
                        "adults": 2,
                    }
                },
            )

        assert result.data.listings[0].hotel_name == "Test Hotel"
        assert result.data.listings[0].estimated_price_total_usd == 600.0

    async def test_tool_error_is_raised_with_real_message(self, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "_search_hotels",
            lambda query: DomainToolError(
                tool_name="search_hotels",
                error_type="duffel_api_error",
                message="Duffel Stays API returned 403: not enabled for this account",
                retryable=False,
            ),
        )

        async with Client(server_module.mcp) as client:
            with pytest.raises(MCPToolError, match="duffel_api_error"):
                await client.call_tool(
                    "search_hotels",
                    {
                        "query": {
                            "city": "Atlanta",
                            "check_in": _depart_date_str(),
                            "check_out": (date.today() + timedelta(days=33)).isoformat(),
                        }
                    },
                )


# ---------------------------------------------------------------------------
# check_visa_requirements
# ---------------------------------------------------------------------------


class TestCheckVisaRequirementsTool:
    async def test_success_always_carries_disclaimer(self, monkeypatch):
        fake_result = VisaCheckResult(
            passport_country="India",
            destination_country="Thailand",
            purpose=TravelPurpose.TOURISM,
            visa_required=False,
            summary="Indian passport holders can enter Thailand visa-free for tourism up to "
            "30 days.",
            model="mock",
        )
        monkeypatch.setattr(server_module, "_check_visa_requirements", lambda query: fake_result)

        async with Client(server_module.mcp) as client:
            result = await client.call_tool(
                "check_visa_requirements",
                {"query": {"passport_country": "India", "destination_country": "Thailand"}},
            )

        assert result.data.visa_required is False
        assert result.data.confidence_level == "informational_only"
        assert "NOT an authoritative" in result.data.disclaimer

    async def test_malformed_llm_response_is_raised_with_real_message(self, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "_check_visa_requirements",
            lambda query: DomainToolError(
                tool_name="check_visa_requirements",
                error_type="malformed_llm_response",
                message="Groq returned a response that couldn't be parsed as expected JSON",
                retryable=True,
            ),
        )

        async with Client(server_module.mcp) as client:
            with pytest.raises(MCPToolError, match="malformed_llm_response"):
                await client.call_tool(
                    "check_visa_requirements",
                    {"query": {"passport_country": "India", "destination_country": "Thailand"}},
                )


# ---------------------------------------------------------------------------
# create_trip — real in-memory SQLite
# ---------------------------------------------------------------------------


class TestCreateTripTool:
    async def test_creates_trip_in_draft_status(self, sqlite_session_override):
        async with Client(server_module.mcp) as client:
            trip = await _mcp_create_trip(client)

        assert trip.origin_iata == "JFK"
        assert trip.destination_iata == "ATL"
        assert trip.status == "draft"
        assert trip.adults == 1

    async def test_optional_fields_default_correctly(self, sqlite_session_override):
        async with Client(server_module.mcp) as client:
            trip = await _mcp_create_trip(
                client, max_budget_usd=1500.0, requester_email="test@example.com"
            )

        assert trip.max_budget_usd == 1500.0
        assert trip.requester_email == "test@example.com"
        assert trip.return_date is None


# ---------------------------------------------------------------------------
# propose_flight_booking — real in-memory SQLite
# ---------------------------------------------------------------------------


class TestProposeFlightBookingTool:
    async def test_creates_pending_booking(self, sqlite_session_override, flight_offer):
        async with Client(server_module.mcp) as client:
            trip = await _mcp_create_trip(client)
            result = await client.call_tool(
                "propose_flight_booking",
                {"trip_id": trip.id, "offer": flight_offer.model_dump(mode="json")},
            )

        assert result.data.status == "pending_approval"
        assert result.data.was_existing is False

    async def test_repeated_call_is_idempotent(self, sqlite_session_override, flight_offer):
        async with Client(server_module.mcp) as client:
            trip = await _mcp_create_trip(client)
            args = {"trip_id": trip.id, "offer": flight_offer.model_dump(mode="json")}
            first = await client.call_tool("propose_flight_booking", args)
            second = await client.call_tool("propose_flight_booking", args)

        assert first.data.booking_id == second.data.booking_id
        assert first.data.was_existing is False
        assert second.data.was_existing is True

    async def test_invalid_trip_id_raised_as_mcp_error(self, sqlite_session_override, flight_offer):
        async with Client(server_module.mcp) as client:
            with pytest.raises(MCPToolError, match="invalid_trip_id"):
                await client.call_tool(
                    "propose_flight_booking",
                    {"trip_id": "not-a-valid-uuid", "offer": flight_offer.model_dump(mode="json")},
                )


# ---------------------------------------------------------------------------
# propose_hotel_booking — real in-memory SQLite
# ---------------------------------------------------------------------------


class TestProposeHotelBookingTool:
    async def test_creates_pending_hotel_booking(self, sqlite_session_override, hotel_listing):
        check_in = date.today() + timedelta(days=30)
        check_out = date.today() + timedelta(days=33)

        async with Client(server_module.mcp) as client:
            trip = await _mcp_create_trip(client)
            result = await client.call_tool(
                "propose_hotel_booking",
                {
                    "trip_id": trip.id,
                    "listing": hotel_listing.model_dump(mode="json"),
                    "check_in": check_in.isoformat(),
                    "check_out": check_out.isoformat(),
                },
            )

        assert result.data.status == "pending_approval"
        assert result.data.was_existing is False

    async def test_invalid_trip_id_raised_as_mcp_error(
        self, sqlite_session_override, hotel_listing
    ):
        check_in = date.today() + timedelta(days=30)
        check_out = date.today() + timedelta(days=33)

        async with Client(server_module.mcp) as client:
            with pytest.raises(MCPToolError, match="invalid_trip_id"):
                await client.call_tool(
                    "propose_hotel_booking",
                    {
                        "trip_id": "not-a-valid-uuid",
                        "listing": hotel_listing.model_dump(mode="json"),
                        "check_in": check_in.isoformat(),
                        "check_out": check_out.isoformat(),
                    },
                )


class TestEstimateGroundTransportTool:
    async def test_success_returns_estimate(self, monkeypatch):
        from app.tools.schemas import GroundTransportEstimateResult

        fake_result = GroundTransportEstimateResult(
            origin_resolved_name="Home",
            origin_latitude=40.6413,
            origin_longitude=-73.7781,
            destination_resolved_name="JFK Airport",
            destination_latitude=40.6413,
            destination_longitude=-73.7781,
            distance_km=15.0,
            estimated_cost_usd_low=8.0,
            estimated_cost_usd_high=14.0,
        )
        monkeypatch.setattr(server_module, "_estimate_ground_transport", lambda query: fake_result)

        async with Client(server_module.mcp) as client:
            result = await client.call_tool(
                "estimate_ground_transport",
                {"query": {"origin_city": "Home", "destination_city": "JFK Airport"}},
            )

        assert result.data.distance_km == 15.0
        assert "NOT a real fare" in result.data.disclaimer

    async def test_location_not_found_is_raised_with_real_message(self, monkeypatch):
        monkeypatch.setattr(
            server_module,
            "_estimate_ground_transport",
            lambda query: DomainToolError(
                tool_name="estimate_ground_transport",
                error_type="location_not_found",
                message="Couldn't find a origin location matching 'Nowhereville'.",
                retryable=False,
            ),
        )

        async with Client(server_module.mcp) as client:
            with pytest.raises(MCPToolError, match="location_not_found"):
                await client.call_tool(
                    "estimate_ground_transport",
                    {"query": {"origin_city": "Nowhereville", "destination_city": "JFK Airport"}},
                )


# ---------------------------------------------------------------------------
# Server-wide sanity
# ---------------------------------------------------------------------------


class TestServerRegistration:
    async def test_all_eight_tools_are_registered(self):
        async with Client(server_module.mcp) as client:
            tools = await client.list_tools()

        names = {t.name for t in tools}
        assert names == {
            "search_flights",
            "get_weather_forecast",
            "search_hotels",
            "check_visa_requirements",
            "estimate_ground_transport",
            "create_trip",
            "propose_flight_booking",
            "propose_hotel_booking",
        }
