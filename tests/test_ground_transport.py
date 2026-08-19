"""
Tests for app/tools/ground_transport.py.

Mocks the Open-Meteo geocoding HTTP call via pytest-httpx, same pattern
already used for the other httpx-backed tools in this project — no real
network call, no dependency on the geocoding API being reachable during
test runs.
"""

from __future__ import annotations

import pytest
from app.tools.geocoding import _GEOCODING_URL

from app.tools.ground_transport import _haversine_km, estimate_ground_transport
from app.tools.schemas import GroundTransportEstimateInput, ToolError


def _geocoding_response(
    name: str, lat: float, lon: float, admin1: str = "", country: str = ""
) -> dict:
    return {
        "results": [
            {"name": name, "latitude": lat, "longitude": lon, "admin1": admin1, "country": country}
        ]
    }


class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine_km(40.0, -73.0, 40.0, -73.0) == 0.0

    def test_known_distance_jfk_to_lga(self):
        # Real-world JFK<->LGA distance is ~17-18km straight-line.
        km = _haversine_km(40.6413, -73.7781, 40.7769, -73.8740)
        assert 16.0 < km < 19.0


class TestEstimateGroundTransport:
    def test_success_with_city_names(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_GEOCODING_URL}?name=Home+Address&count=1&language=en&format=json",
            json=_geocoding_response("Home Address", 40.6413, -73.7781, country="US"),
        )
        httpx_mock.add_response(
            url=f"{_GEOCODING_URL}?name=JFK+Airport&count=1&language=en&format=json",
            json=_geocoding_response("JFK Airport", 40.7769, -73.8740, country="US"),
        )

        query = GroundTransportEstimateInput(
            origin_city="Home Address", destination_city="JFK Airport"
        )
        result = estimate_ground_transport(query)

        assert not isinstance(result, ToolError)
        assert result.distance_km > 0
        assert result.estimated_cost_usd_low <= result.estimated_cost_usd_high
        assert "NOT a real fare" in result.disclaimer

    def test_success_with_explicit_lat_lon_skips_geocoding(self, httpx_mock):
        # No httpx_mock.add_response registered — if the tool tried to
        # geocode anything here, pytest-httpx would raise on the
        # unexpected request, failing this test.
        query = GroundTransportEstimateInput(
            origin_latitude=40.6413,
            origin_longitude=-73.7781,
            destination_latitude=40.7769,
            destination_longitude=-73.8740,
        )
        result = estimate_ground_transport(query)

        assert not isinstance(result, ToolError)
        assert result.origin_resolved_name == "40.6413, -73.7781"

    def test_same_origin_and_destination_still_respects_min_fare(self, httpx_mock):
        query = GroundTransportEstimateInput(
            origin_latitude=40.6413,
            origin_longitude=-73.7781,
            destination_latitude=40.6413,
            destination_longitude=-73.7781,
        )
        result = estimate_ground_transport(query)

        assert not isinstance(result, ToolError)
        assert result.distance_km == 0.0
        assert result.estimated_cost_usd_low >= 0  # min fare floor applies, never negative

    def test_location_not_found_returns_tool_error(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_GEOCODING_URL}?name=Nonexistent+Place+Xyz&count=1&language=en&format=json",
            json={"results": []},
        )
        query = GroundTransportEstimateInput(
            origin_city="Nonexistent Place Xyz",
            destination_latitude=40.7769,
            destination_longitude=-73.8740,
        )
        result = estimate_ground_transport(query)

        assert isinstance(result, ToolError)
        assert result.error_type == "location_not_found"
        assert result.tool_name == "estimate_ground_transport"

    def test_geocoding_api_error_returns_tool_error(self, httpx_mock):
        httpx_mock.add_response(
            url=f"{_GEOCODING_URL}?name=Somewhere&count=1&language=en&format=json",
            status_code=400,
            text="bad request",
        )
        query = GroundTransportEstimateInput(
            origin_city="Somewhere",
            destination_latitude=40.7769,
            destination_longitude=-73.8740,
        )
        result = estimate_ground_transport(query)

        assert isinstance(result, ToolError)
        assert result.error_type == "geocoding_api_error"
        assert result.retryable is False  # 400 is not in the retryable set


class TestGroundTransportEstimateInputValidation:
    def test_both_origin_city_and_coords_rejected(self):
        with pytest.raises(ValueError, match="not both"):
            GroundTransportEstimateInput(
                origin_city="Home",
                origin_latitude=40.0,
                origin_longitude=-73.0,
                destination_city="Airport",
            )

    def test_neither_destination_city_nor_coords_rejected(self):
        with pytest.raises(ValueError, match="provide either destination_city"):
            GroundTransportEstimateInput(origin_city="Home")

    def test_partial_origin_coords_rejected(self):
        with pytest.raises(ValueError, match="must both be provided together"):
            GroundTransportEstimateInput(
                origin_latitude=40.0,
                destination_city="Airport",
            )