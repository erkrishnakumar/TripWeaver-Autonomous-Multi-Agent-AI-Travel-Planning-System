"""
Shared data contracts for TripWeaver.

These schemas are the single source of truth for the shape of data moving
between tools, agents, the MCP server, and the API layer. Define changes
here first — everything downstream depends on these.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


class CabinClass(StrEnum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class FlightSearchInput(BaseModel):
    """Input contract for search_flights()."""

    origin: str = Field(..., min_length=3, max_length=3, description="IATA airport/city code")
    destination: str = Field(..., min_length=3, max_length=3, description="IATA airport/city code")
    depart_date: date
    return_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    cabin_class: CabinClass = CabinClass.ECONOMY
    max_budget_usd: float | None = Field(default=None, gt=0)

    @field_validator("origin", "destination")
    @classmethod
    def uppercase_iata(cls, v: str) -> str:
        return v.upper()

    @field_validator("return_date")
    @classmethod
    def return_after_depart(cls, v: date | None, info: ValidationInfo) -> date | None:
        depart = info.data.get("depart_date")
        if v is not None and depart is not None and v < depart:
            raise ValueError("return_date cannot be before depart_date")
        return v


class FlightSegment(BaseModel):
    """A single flown leg (e.g. one takeoff/landing) within an offer."""

    carrier_iata: str
    carrier_name: str
    flight_number: str
    origin_iata: str
    destination_iata: str
    departs_at: datetime
    arrives_at: datetime


class FlightOffer(BaseModel):
    """Output contract: one bookable flight offer."""

    offer_id: str
    total_price_usd: float
    price_currency_original: str = "USD"
    cabin_class: CabinClass
    stops_outbound: int
    segments: list[FlightSegment]
    expires_at: datetime | None = Field(
        default=None, description="Offers expire; re-fetch after this time before booking"
    )
    passenger_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Duffel's own generated passenger ids for this offer -- REQUIRED "
            "verbatim when creating a real order later (POST /air/orders); "
            "Duffel rejects arbitrary passenger ids, they must match what "
            "it generated at offer-request time. See create_flight_order()."
        ),
    )


class FlightSearchResult(BaseModel):
    """Output contract for search_flights()."""

    query: FlightSearchInput
    offers: list[FlightOffer]
    provider: str = "duffel"
    is_sandbox: bool = True
    is_mock: bool = False


class WeatherSearchInput(BaseModel):
    """Input contract for get_weather_forecast().

    Accepts EITHER a city name OR explicit lat/lon — not both, not neither.
    City is geocoded internally via Open-Meteo's free geocoding API.
    """

    city: str | None = Field(default=None, min_length=1)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    start_date: date
    end_date: date

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info: ValidationInfo) -> date:
        start = info.data.get("start_date")
        if start is not None and v < start:
            raise ValueError("end_date cannot be before start_date")
        return v

    @model_validator(mode="after")
    def exactly_one_location(self) -> WeatherSearchInput:
        has_city = self.city is not None
        has_coords = self.latitude is not None and self.longitude is not None
        partial_coords = (self.latitude is None) != (self.longitude is None)

        if partial_coords:
            raise ValueError("latitude and longitude must both be provided together")
        if has_city and has_coords:
            raise ValueError("provide either city or lat/lon, not both")
        if not has_city and not has_coords:
            raise ValueError("provide either city or both latitude and longitude")
        return self


class DailyForecast(BaseModel):
    """Forecast for a single calendar day."""

    date: date
    temp_max_c: float
    temp_min_c: float
    precipitation_probability_pct: int | None = None
    weather_code: int
    weather_description: str


class WeatherForecastResult(BaseModel):
    """Output contract for get_weather_forecast()."""

    resolved_location_name: str
    latitude: float
    longitude: float
    daily: list[DailyForecast]
    provider: str = "open-meteo"


class ChildGuest(BaseModel):
    """A child traveler. Duffel requires an age per child, not just a count —
    rates and eligibility can depend on it."""

    age: int = Field(ge=0, le=17)


class HotelSearchInput(BaseModel):
    """Input contract for search_hotels().

    Accepts EITHER a city name OR explicit lat/lon — not both, not neither —
    same convention as WeatherSearchInput. City is geocoded internally via
    the same Open-Meteo geocoding lookup weather.py already uses, so there's
    only one geocoding code path in the whole app.

    Field names below (rooms, location.radius, guests with per-child age)
    match Duffel's real POST /stays/search contract, verified against
    Duffel's own docs — see app/tools/hotels.py module docstring for the
    verification note.
    """

    city: str | None = Field(default=None, min_length=1)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    check_in: date
    check_out: date
    adults: int = Field(default=1, ge=1, le=9)
    children: list[ChildGuest] = Field(
        default_factory=list, description="One entry per child, each with their age"
    )
    rooms: int = Field(default=1, ge=1, le=9)
    max_budget_usd_per_night: float | None = Field(default=None, gt=0)
    radius_km: int = Field(default=5, ge=1, le=100)

    @field_validator("check_out")
    @classmethod
    def check_out_after_check_in(cls, v: date, info: ValidationInfo) -> date:
        check_in = info.data.get("check_in")
        if check_in is not None and v <= check_in:
            raise ValueError("check_out must be after check_in")
        return v

    @model_validator(mode="after")
    def exactly_one_location(self) -> HotelSearchInput:
        has_city = self.city is not None
        has_coords = self.latitude is not None and self.longitude is not None
        partial_coords = (self.latitude is None) != (self.longitude is None)

        if partial_coords:
            raise ValueError("latitude and longitude must both be provided together")
        if has_city and has_coords:
            raise ValueError("provide either city or lat/lon, not both")
        if not has_city and not has_coords:
            raise ValueError("provide either city or both latitude and longitude")
        return self


class HotelListing(BaseModel):
    """One accommodation returned by a Stays search.

    IMPORTANT: this is an ESTIMATED listing, not a guaranteed bookable
    offer. `estimated_price_total_usd` mirrors Duffel's own
    cheapest_rate_total_amount, which their docs explicitly describe as a
    "best effort computation" that "is not guaranteed to be accurate" and
    "can change when fetching rates." Treat this as good enough for
    browsing/comparing, but re-fetch real rates (a separate tool, not part
    of search_hotels()) before quoting a firm price or booking.
    """

    search_result_id: str
    hotel_name: str
    hotel_rating: float | None = None
    review_score: float | None = None
    address_line: str | None = None
    city_name: str | None = None
    latitude: float
    longitude: float
    estimated_price_total_usd: float
    rate_id: str | None = Field(
        default=None,
        description=(
            "The cheapest bookable rate's own id (accommodation.rooms[].rates[].id "
            "in Duffel's real fetch_all_rates response) -- only present after "
            "get_hotel_rate() (the fetch_all_rates step), never from search_hotels() "
            "directly, which is too lightweight to include individual rates. "
            "REQUIRED (not inventable) to get a real quote -- see get_hotel_quote()."
        ),
    )
    price_currency_original: str = "USD"
    nights: int
    expires_at: datetime | None = Field(
        default=None,
        description="This search result expires; re-search or fetch rates before booking",
    )


class HotelGuestDetails(BaseModel):
    """Guest name required by Duffel's real Stays booking contract
    (POST /stays/bookings) -- lighter than PassengerDetails/DriverDetails
    since Stays only wants given_name/family_name per guest; email/phone
    are booking-level, not per-guest (see create_hotel_booking())."""

    given_name: str = Field(..., min_length=1)
    family_name: str = Field(..., min_length=1)


class HotelQuoteResult(BaseModel):
    """Output of get_hotel_quote() -- a FIRM, bookable price for one rate.
    Same principle as CarQuoteResult: always show/use THIS price before a
    human approves anything, never the original rate's estimated price."""

    quote_id: str
    rate_id: str
    total_price_usd: float
    price_currency_original: str = "USD"
    expires_at: datetime | None = None


class HotelSearchResult(BaseModel):
    """Output contract for search_hotels()."""

    query: HotelSearchInput
    resolved_location_name: str
    listings: list[HotelListing]
    provider: str = "duffel"
    is_sandbox: bool = True
    is_mock: bool = False


class ToolError(BaseModel):
    """Standard error shape returned by tools instead of raising raw exceptions
    up to the agent layer — lets an agent reason about failure instead of crashing."""

    tool_name: str
    error_type: str
    message: str
    retryable: bool = False


class TravelPurpose(StrEnum):
    TOURISM = "tourism"
    BUSINESS = "business"
    TRANSIT = "transit"


class VisaCheckInput(BaseModel):
    """Input contract for check_visa_requirements().

    Countries are given as plain names (e.g. "India", "Thailand") rather
    than ISO codes — this is a natural-language lookup by design, since the
    underlying answer comes from an LLM's general knowledge rather than a
    structured database with codified country keys.
    """

    passport_country: str = Field(..., min_length=2)
    destination_country: str = Field(..., min_length=2)
    purpose: TravelPurpose = TravelPurpose.TOURISM


class VisaCheckResult(BaseModel):
    """Output contract for check_visa_requirements().

    IMPORTANT: this is an LLM-generated, INFORMATIONAL-ONLY answer, not an
    authoritative or real-time source. Visa rules change (new agreements,
    e-visa programs, suspensions) and this reflects only the model's
    general training knowledge, with no guarantee of currency or accuracy.
    confidence_level is always "informational_only" — there is no tier of
    this tool's output that should be treated as verified or official.
    Every result carries a disclaimer directing the traveler to check an
    official government source before relying on this for travel plans.
    """

    passport_country: str
    destination_country: str
    purpose: TravelPurpose
    visa_required: bool | None = Field(
        default=None,
        description=(
            "True/False if the model gave a clear answer, None if the answer "
            "was ambiguous, conditional, or the model was unsure — treat None "
            "as 'we don't know, check an official source', not as an error."
        ),
    )
    summary: str = Field(description="The model's plain-language explanation")
    confidence_level: str = "informational_only"
    disclaimer: str = (
        "This is a general, AI-generated estimate based on commonly known travel "
        "rules — it is NOT an authoritative or real-time source. Visa policies "
        "change without notice. Always verify with the destination country's "
        "official immigration website or embassy before booking or traveling."
    )
    provider: str = "groq"
    model: str


class ProposeFlightBookingInput(BaseModel):
    """Input contract for propose_booking() when booking_type is FLIGHT.

    Takes a FlightOffer directly from search_flights() — propose_booking()
    persists it as a FlightOption row itself; nothing needs to have written
    that row beforehand.
    """

    trip_id: str = Field(description="UUID string of an existing Trip")
    offer: FlightOffer


class ProposeHotelBookingInput(BaseModel):
    """Input contract for propose_booking() when booking_type is HOTEL.

    Takes a HotelListing directly from search_hotels(). Remember: a
    HotelListing carries an ESTIMATED price (see HotelListing's own
    docstring) — propose_booking() persists whatever price is given here,
    so if a firm rate hasn't been fetched yet, the human approver is
    approving an estimate, not a guaranteed price. That's worth surfacing
    in the UI/agent layer, not something this tool can fix on its own.
    """

    trip_id: str = Field(description="UUID string of an existing Trip")
    listing: HotelListing
    check_in: date
    check_out: date


class ProposeBookingResult(BaseModel):
    """Output contract for propose_booking().

    A successful result means a PENDING_APPROVAL row now exists in the
    database — it does NOT mean anything was booked. No real booking
    endpoint is ever called by this tool; that is a deliberate, permanent
    property of propose_booking(), not a current limitation.
    """

    booking_id: str
    approval_id: str
    status: str
    idempotency_key: str
    was_existing: bool = Field(
        description=(
            "True if this call matched an already-pending booking for the "
            "same trip+offer (idempotent replay), rather than creating a new one."
        )
    )


class BookingDecisionResult(BaseModel):
    """Output contract for confirm_booking()/reject_booking() — Gate 2.

    provider_booking_reference is only ever set after a REAL provider
    booking succeeded (confirm_booking's success path); it is always None
    for reject_booking() and for a confirm_booking() call that failed
    against the real provider (booking_status will be "booking_failed" in
    that case, not "booked" — see confirm_booking()'s own docstring for why
    a failed provider call is never disguised as success).
    """

    booking_id: str
    approval_id: str
    booking_status: str
    provider_booking_reference: str | None = None
    message: str


class TripSummary(BaseModel):
    """Output contract for create_trip() when exposed via the MCP server (and,
    later, the API layer).

    app.tools.create_trip.create_trip() returns a live SQLAlchemy Trip ORM
    object, which is only safely readable while its owning session is open
    and isn't a JSON-serializable shape a client should ever depend on
    directly. This mirrors Trip's public, non-relationship columns as a
    plain schema — the same "define it here first" principle as every other
    contract in this file. Deliberately excludes Trip.notes and the
    relationship collections (itineraries/flight_options/hotel_options/
    bookings): those are internal/future-facing, not part of what
    create_trip() hands back today.
    """

    id: str
    status: str
    origin_iata: str
    destination_iata: str
    depart_date: date
    return_date: date | None = None
    adults: int
    max_budget_usd: float | None = None
    requester_email: str | None = None


class GroundTransportEstimateInput(BaseModel):
    """Input contract for estimate_ground_transport().

    Two arbitrary points — e.g. "home" and an airport, or an airport and a
    hotel — each specified as EITHER a place/city name OR explicit lat/lon,
    same convention as WeatherSearchInput/HotelSearchInput.

    This is deliberately NOT a bookable request. TripWeaver does not
    integrate a ride-hailing API for these legs: Ola and Rapido have no
    public booking API at all, and Uber's requires an enterprise
    partnership (Uber for Business / Guest Rides), not a self-serve
    sandbox token like Duffel's. Duffel Cars was also considered and
    rejected for this use case — it's self-drive car RENTAL (Avis/Hertz/
    Sixt/etc.), not a chauffeured point-to-point transfer; using it to get
    dropped at an airport counter would be technically possible but
    practically wrong. Given no real booking API fits, this tool only ever
    returns a rough, disclaimed cost estimate.
    """

    origin_city: str | None = Field(default=None, min_length=1)
    origin_latitude: float | None = Field(default=None, ge=-90, le=90)
    origin_longitude: float | None = Field(default=None, ge=-180, le=180)

    destination_city: str | None = Field(default=None, min_length=1)
    destination_latitude: float | None = Field(default=None, ge=-90, le=90)
    destination_longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def exactly_one_origin_location(self) -> GroundTransportEstimateInput:
        has_city = self.origin_city is not None
        has_coords = self.origin_latitude is not None and self.origin_longitude is not None
        partial_coords = (self.origin_latitude is None) != (self.origin_longitude is None)

        if partial_coords:
            raise ValueError("origin_latitude and origin_longitude must both be provided together")
        if has_city and has_coords:
            raise ValueError("provide either origin_city or origin lat/lon, not both")
        if not has_city and not has_coords:
            raise ValueError(
                "provide either origin_city or both origin_latitude and origin_longitude"
            )
        return self

    @model_validator(mode="after")
    def exactly_one_destination_location(self) -> GroundTransportEstimateInput:
        has_city = self.destination_city is not None
        has_coords = (
            self.destination_latitude is not None and self.destination_longitude is not None
        )
        partial_coords = (self.destination_latitude is None) != (self.destination_longitude is None)

        if partial_coords:
            raise ValueError(
                "destination_latitude and destination_longitude must both be provided together"
            )
        if has_city and has_coords:
            raise ValueError("provide either destination_city or destination lat/lon, not both")
        if not has_city and not has_coords:
            raise ValueError(
                "provide either destination_city or both"
                "destination_latitude and destination_longitude"
            )
        return self


class GroundTransportEstimateResult(BaseModel):
    """Output contract for estimate_ground_transport().

    THIS IS NEVER A BOOKABLE FARE — see GroundTransportEstimateInput's
    docstring for why no ride-hailing API is integrated. distance_km is
    straight-line (haversine) distance with a rough road-distance
    correction factor applied, NOT a real route distance — no routing API
    is called. estimated_cost_usd_low/high is a budgeting range, not a
    quote. Always relay `disclaimer` to the traveler alongside the range.
    """

    origin_resolved_name: str
    origin_latitude: float
    origin_longitude: float
    destination_resolved_name: str
    destination_latitude: float
    destination_longitude: float
    distance_km: float
    estimated_cost_usd_low: float
    estimated_cost_usd_high: float
    disclaimer: str = (
        "This is a rough, straight-line-distance estimate — NOT a real fare or a "
        "booking. Actual cost depends on your local ride app, route, traffic, and "
        "surge pricing. Book your ride separately (e.g. Uber, Ola, Rapido, or a "
        "local taxi/auto)."
    )
    provider: str = "estimate"


class CarRentalPaymentType(StrEnum):
    """Duffel Cars' three payment models — surfaced verbatim to the human
    approver, since it changes what "approving this" actually commits to
    (money charged now vs. a card held vs. nothing at all until the counter)."""

    PREPAID = "prepaid"
    GUARANTEE = "guarantee"
    POSTPAID = "postpaid"


class PassengerTitle(StrEnum):
    MR = "mr"
    MRS = "mrs"
    MS = "ms"
    MISS = "miss"


class PassengerGender(StrEnum):
    MALE = "m"
    FEMALE = "f"


class PassengerDetails(BaseModel):
    """Passenger PII required by Duffel's real Orders booking contract
    (POST /air/orders) -- same sensitivity class as DriverDetails (date of
    birth, phone number); see that class's docstring for the standing
    auth/closed-user-group requirement this reinforces.

    passenger_id must be one of the ids from the FlightOffer being booked
    (FlightOffer.passenger_ids) -- NOT an id you invent. Duffel rejects an
    order whose passenger ids don't match ones it generated at
    offer-request time; see create_flight_order()'s docstring."""

    passenger_id: str = Field(..., min_length=1)
    title: PassengerTitle
    gender: PassengerGender
    given_name: str = Field(..., min_length=1)
    family_name: str = Field(..., min_length=1)
    date_of_birth: date
    email: str = Field(..., min_length=3)
    phone_number: str = Field(..., min_length=3)


class DriverDetails(BaseModel):
    """Driver PII required by Duffel's real Cars booking contract
    (POST /cars/bookings). This is more sensitive than anything else
    TripWeaver currently stores (date of birth, phone number) — handle
    accordingly; see the Cars rollout notes in docs/TripWeaver_Roadmap.md
    for the standing auth/closed-user-group requirement this reinforces."""

    given_name: str = Field(..., min_length=1)
    family_name: str = Field(..., min_length=1)
    date_of_birth: date
    email: str = Field(..., min_length=3)
    phone_number: str = Field(..., min_length=3)


class CarRentalSearchInput(BaseModel):
    """Input contract for search_car_rentals().

    Pickup is EITHER a city name OR explicit lat/lon, same convention as
    every other search-by-place input in this file. Dropoff is optional —
    if omitted entirely, it defaults to the pickup location (a same-location
    rental), matching the common case; if partially given, that's an error,
    same "all or nothing" rule as every other location pair here.

    driver_age and driver_country_code are required by Duffel's real
    /cars/searches contract, not optional extras — some suppliers restrict
    or surcharge based on driver age, and eligibility can depend on country
    of residence, so omitting these would silently misprice or hide results.
    """

    pickup_city: str | None = Field(default=None, min_length=1)
    pickup_latitude: float | None = Field(default=None, ge=-90, le=90)
    pickup_longitude: float | None = Field(default=None, ge=-180, le=180)

    dropoff_city: str | None = Field(default=None, min_length=1)
    dropoff_latitude: float | None = Field(default=None, ge=-90, le=90)
    dropoff_longitude: float | None = Field(default=None, ge=-180, le=180)

    pickup_at: datetime
    dropoff_at: datetime
    radius_km: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Search radius around pickup/dropoff, in km. Duffel's real "
        "POST /cars/search caps this at 10 — verified via a live 422 "
        "('radius must be less than or equal to 10'), unlike Stays' radius "
        "(HotelSearchInput), which allows up to 100.",
    )

    driver_age: int = Field(ge=18, le=99)
    driver_country_code: str = Field(..., min_length=2, max_length=2)

    @field_validator("dropoff_at")
    @classmethod
    def dropoff_after_pickup(cls, v: datetime, info: ValidationInfo) -> datetime:
        pickup_at = info.data.get("pickup_at")
        if pickup_at is not None and v <= pickup_at:
            raise ValueError("dropoff_at must be after pickup_at")
        return v

    @model_validator(mode="after")
    def exactly_one_pickup_location(self) -> CarRentalSearchInput:
        has_city = self.pickup_city is not None
        has_coords = self.pickup_latitude is not None and self.pickup_longitude is not None
        partial_coords = (self.pickup_latitude is None) != (self.pickup_longitude is None)

        if partial_coords:
            raise ValueError("pickup_latitude and pickup_longitude must both be provided together")
        if has_city and has_coords:
            raise ValueError("provide either pickup_city or pickup lat/lon, not both")
        if not has_city and not has_coords:
            raise ValueError(
                "provide either pickup_city or both pickup_latitude and pickup_longitude"
            )
        return self

    @model_validator(mode="after")
    def dropoff_all_or_nothing(self) -> CarRentalSearchInput:
        has_city = self.dropoff_city is not None
        has_coords = self.dropoff_latitude is not None and self.dropoff_longitude is not None
        partial_coords = (self.dropoff_latitude is None) != (self.dropoff_longitude is None)

        if partial_coords:
            raise ValueError(
                "dropoff_latitude and dropoff_longitude must both be provided together"
            )
        if has_city and has_coords:
            raise ValueError("provide either dropoff_city or dropoff lat/lon, not both")
        # Omitting dropoff entirely is valid — it defaults to the pickup
        # location in search_car_rentals(), a same-location rental.
        return self


class CarRateOption(BaseModel):
    """One rate returned by a Cars search — an ESTIMATE, not a firm bookable
    price. Duffel's own docs warn the final price on a quote can differ from
    the rate price, so re-fetch a quote (get_car_rental_quote()) before
    presenting a firm number for approval — same "estimate vs. firm" caveat
    HotelListing already carries for Stays."""

    rate_id: str
    car_description: str = Field(description='e.g. "Compact - Toyota Corolla or similar"')
    supplier_name: str
    payment_type: CarRentalPaymentType
    estimated_price_total_usd: float
    price_currency_original: str = "USD"
    pickup_location_name: str
    dropoff_location_name: str
    pickup_at: datetime
    dropoff_at: datetime


class CarRentalSearchResult(BaseModel):
    """Output contract for search_car_rentals()."""

    query: CarRentalSearchInput
    resolved_pickup_location_name: str
    resolved_dropoff_location_name: str
    rates: list[CarRateOption]
    provider: str = "duffel"
    is_sandbox: bool = True
    is_mock: bool = False


class CarQuoteInput(BaseModel):
    """Input contract for get_car_rental_quote() — the "Quote" step of
    Duffel's Search -> Quote -> Booking Cars flow."""

    rate_id: str = Field(
        description="A rate_id from a CarRateOption returned by search_car_rentals()"
    )


class CarQuoteResult(BaseModel):
    """Output contract for get_car_rental_quote().

    This IS the firm, bookable price — unlike CarRateOption's estimate.
    Duffel's docs explicitly say to display this price, not the original
    rate's price, before asking a human to approve anything. expires_at
    matters here more than on a flight/hotel offer: a stale quote_id used
    for booking after expiry will be rejected by Duffel.
    """

    quote_id: str
    rate_id: str
    total_price_usd: float
    price_currency_original: str = "USD"
    payment_type: CarRentalPaymentType
    expires_at: datetime | None = Field(
        default=None, description="Quotes expire; re-fetch after this time before booking"
    )


class ProposeCarBookingInput(BaseModel):
    """Input contract for propose_booking() when booking_type is CAR.

    Takes BOTH the original CarRateOption (for display fields — car
    description, supplier, pickup/dropoff names and times — none of which
    Duffel's quote response repeats) AND the CarQuoteResult (the FIRM price,
    not the rate's estimate) plus the driver's details, which Duffel's real
    booking endpoint requires but which search/quote never needed.
    propose_booking() persists this as a CarRentalOption row itself — same
    pattern as the flight/hotel inputs.
    """

    trip_id: str = Field(description="UUID string of an existing Trip")
    rate: CarRateOption
    quote: CarQuoteResult
    driver: DriverDetails

    @model_validator(mode="after")
    def quote_matches_rate(self) -> ProposeCarBookingInput:
        if self.quote.rate_id != self.rate.rate_id:
            raise ValueError(
                f"quote.rate_id ({self.quote.rate_id!r}) does not match "
                f"rate.rate_id ({self.rate.rate_id!r}) — this quote was not fetched "
                "for this rate."
            )
        return self
