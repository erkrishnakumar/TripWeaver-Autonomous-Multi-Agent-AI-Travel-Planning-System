"""
estimate_ground_transport() — deterministic tool function, no LLM, no
booking, no DB.

TripWeaver deliberately does NOT integrate a ride-hailing API for
home<->airport / airport<->hotel legs. Two options were considered and
rejected before building this:

  - Duffel Cars (a real product, launched April 2026) — but it's self-drive
    car RENTAL (Avis/Hertz/Sixt/etc: you take the keys and drive yourself),
    not a chauffeured point-to-point transfer. Using it for a one-off
    airport hop would be technically possible (Duffel Cars supports
    one-way rentals with different pickup/dropoff locations) but
    practically wrong — nobody rents a self-drive car just to get dropped
    at an airport counter.
  - Real ride-hailing APIs — Ola and Rapido have no public booking API at
    all; Uber's requires an enterprise partnership (Uber for Business /
    Guest Rides), not a self-serve sandbox token like Duffel's.

Given neither option fits, this tool gives a rough, clearly-disclaimed cost
ESTIMATE instead of attempting a real booking — same principle as
check_visa_requirements() giving an informational-only answer where no
authoritative source exists, rather than faking precision it doesn't have.
This tool has NO approval-gate / propose_booking() counterpart and never
will: there is nothing here to approve, because nothing here is ever
bookable.

Uses straight-line (haversine) distance with a rough road-distance
correction factor, not a real routed distance — a routing API would be
more accurate, but this is explicitly a rough estimate, not a quote, so
the added dependency/complexity isn't justified. Reuses geocode_city()
(the same geocoding path weather.py and hotels.py already use) for any
city-name input, so there is still exactly one geocoding code path in the
whole app.

Set GROUND_TRANSPORT_RATE_PER_KM_USD / GROUND_TRANSPORT_MIN_FARE_USD in
.env to tune the estimate for a specific market — see app/config.py for
the (deliberately rough, global) defaults.
"""

from __future__ import annotations

import math

import httpx

from app.config import settings
from app.tools.geocoding import GeocodingAPIError, geocode_city
from app.tools.schemas import (
    GroundTransportEstimateInput,
    GroundTransportEstimateResult,
    ToolError,
)

_EARTH_RADIUS_KM = 6371.0

# Straight-line distance always undershoots real road distance (roads
# aren't drawn as straight lines) — this factor gives a rough correction
# toward a more realistic driving distance. Not precise, but more honest
# than presenting pure great-circle distance as if it were the route
# you'd actually drive.
_ROAD_DISTANCE_FACTOR = 1.3


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_KM * c


def _resolve_location(
    city: str | None,
    latitude: float | None,
    longitude: float | None,
    *,
    side: str,
) -> tuple[float, float, str] | ToolError:
    """Resolve one endpoint (origin or destination) to (lat, lon, display_name)."""
    if city is not None:
        try:
            geocoded = geocode_city(city)
        except GeocodingAPIError as e:
            return ToolError(
                tool_name="estimate_ground_transport",
                error_type="geocoding_api_error",
                message=e.message,
                retryable=e.retryable,
            )
        except httpx.TimeoutException:
            return ToolError(
                tool_name="estimate_ground_transport",
                error_type="timeout",
                message="Geocoding API did not respond within 15s",
                retryable=True,
            )
        if geocoded is None:
            return ToolError(
                tool_name="estimate_ground_transport",
                error_type="location_not_found",
                message=f"Couldn't find a {side} location matching '{city}'.",
                retryable=False,
            )
        return geocoded

    # lat/lon were given directly — no display name to resolve, use the
    # coordinates themselves, same convention as weather.py/hotels.py.
    return latitude, longitude, f"{latitude:.4f}, {longitude:.4f}"


def estimate_ground_transport(
    query: GroundTransportEstimateInput,
) -> GroundTransportEstimateResult | ToolError:
    """
    Get a rough, non-bookable cost estimate for a ground-transport leg
    (e.g. home -> airport, or airport -> hotel) between two points, each
    given as a city name or explicit lat/lon.

    THIS NEVER BOOKS ANYTHING and never will — see module docstring for
    why no ride-hailing API is integrated. ALWAYS relay
    GroundTransportEstimateResult.disclaimer to the traveler alongside the
    estimate; treat estimated_cost_usd_low/high as a budgeting range, not
    a quote.

    Returns GroundTransportEstimateResult on success, or ToolError on
    failure (e.g. an unresolvable place name) — same contract as every
    other tool in app/tools/.
    """
    origin = _resolve_location(
        query.origin_city, query.origin_latitude, query.origin_longitude, side="origin"
    )
    if isinstance(origin, ToolError):
        return origin
    origin_lat, origin_lon, origin_name = origin

    destination = _resolve_location(
        query.destination_city,
        query.destination_latitude,
        query.destination_longitude,
        side="destination",
    )
    if isinstance(destination, ToolError):
        return destination
    dest_lat, dest_lon, dest_name = destination

    straight_line_km = _haversine_km(origin_lat, origin_lon, dest_lat, dest_lon)
    distance_km = round(straight_line_km * _ROAD_DISTANCE_FACTOR, 1)

    raw_cost = distance_km * settings.ground_transport_rate_per_km_usd
    cost_low = max(settings.ground_transport_min_fare_usd, round(raw_cost * 0.8, 2))
    cost_high = max(cost_low, round(raw_cost * 1.3, 2))

    return GroundTransportEstimateResult(
        origin_resolved_name=origin_name,
        origin_latitude=origin_lat,
        origin_longitude=origin_lon,
        destination_resolved_name=dest_name,
        destination_latitude=dest_lat,
        destination_longitude=dest_lon,
        distance_km=distance_km,
        estimated_cost_usd_low=cost_low,
        estimated_cost_usd_high=cost_high,
    )


if __name__ == "__main__":
    # Manual smoke test: run `uv run python -m app.tools.ground_transport`
    demo_query = GroundTransportEstimateInput(
        origin_city="Hyderabad",
        destination_city="Secunderabad",
    )
    result = estimate_ground_transport(demo_query)
    if isinstance(result, ToolError):
        print(f"[ERROR] {result.error_type}: {result.message}")
    else:
        print(
            f"{result.origin_resolved_name} -> {result.destination_resolved_name}: "
            f"~{result.distance_km} km, "
            f"${result.estimated_cost_usd_low}-${result.estimated_cost_usd_high}"
        )
        print(f"\n{result.disclaimer}")