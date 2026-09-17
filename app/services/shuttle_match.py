"""Match a flight departure to the best Ecojindu shuttle toward the airport."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ValidationError
from app.core.logging import log_event
from app.core.timeutil import LAGOS
from app.models.enums import TripStatus
from app.models.trip import Trip
from app.schemas.trip import TripOut
from app.services.trips import search_trips, to_trip_out

logger = logging.getLogger("ecojindu.shuttle_match")

PICKUP_CITIES = frozenset({"Umuahia", "Aba"})

#: Destination keywords that identify airport-bound corridors.
AIRPORT_DESTINATION_KEYWORDS = (
    "sam mbakwe",
    "mbakwe",
    "owerri",
    "airport",
    "qow",
)


@dataclass(slots=True)
class ShuttleSuggestion:
    trip: TripOut | None
    fits: bool
    check_in_by: str
    message: str
    alternatives: list[TripOut] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trip": self.trip.model_dump(mode="json") if self.trip else None,
            "fits": self.fits,
            "check_in_by": self.check_in_by,
            "message": self.message,
            "alternatives": [t.model_dump(mode="json") for t in self.alternatives],
        }


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LAGOS)
    return dt


def _trip_arrival(trip: Trip) -> datetime:
    if trip.arrival_estimate is not None:
        return _ensure_aware(trip.arrival_estimate)
    duration = trip.route.duration_mins if trip.route else 90
    return _ensure_aware(trip.departure_datetime) + timedelta(minutes=duration)


def _is_airport_destination(destination: str) -> bool:
    lower = destination.lower()
    return any(k in lower for k in AIRPORT_DESTINATION_KEYWORDS)


def parse_pickup_city(value: str | None) -> str:
    city = (value or "Umuahia").strip().title()
    if city not in PICKUP_CITIES:
        raise ValidationError(
            f"Pickup city must be one of: {', '.join(sorted(PICKUP_CITIES))}.",
            code="invalid_pickup_city",
        )
    return city


async def match_shuttle(
    db: AsyncSession,
    *,
    flight_departure: datetime,
    pickup_city: str = "Umuahia",
    limit_alternatives: int = 5,
) -> ShuttleSuggestion:
    """Pick the latest airport-bound trip that arrives before recommended check-in."""
    city = parse_pickup_city(pickup_city)
    flight_dep = _ensure_aware(flight_departure)
    check_in_by = flight_dep - timedelta(hours=settings.CHECK_IN_BUFFER_HOURS)
    check_in_iso = check_in_by.isoformat()

    # Search the flight's local calendar day (±1 day for late-night edge cases).
    service_day = flight_dep.astimezone(LAGOS).date()
    candidates = await search_trips(
        db,
        origin=city,
        date_from=service_day - timedelta(days=1),
        date_to=service_day,
        include_past=True,
        limit=200,
    )

    airport_trips: list[Trip] = []
    check_in_day = check_in_by.astimezone(LAGOS).date()
    for trip in candidates:
        if trip.status == TripStatus.CANCELLED:
            continue
        dest = trip.route.destination if trip.route else ""
        if not _is_airport_destination(dest):
            continue
        # Origin must mention the pickup city.
        origin = (trip.route.origin_terminal if trip.route else "") or ""
        if city.lower() not in origin.lower():
            continue
        arrival = _trip_arrival(trip)
        # Keep candidates on the check-in calendar day (ignore previous-day trips).
        if arrival.astimezone(LAGOS).date() != check_in_day:
            continue
        airport_trips.append(trip)

    airport_trips.sort(key=lambda t: _trip_arrival(t))

    fitting = [t for t in airport_trips if _trip_arrival(t) <= check_in_by]
    if fitting:
        best = fitting[-1]  # latest arrival still before check-in
        alts = [
            to_trip_out(t, t.route)
            for t in reversed(fitting[:-1])
            if t.id != best.id
        ][:limit_alternatives]
        msg = (
            f"Recommended shuttle arrives before your {settings.CHECK_IN_BUFFER_HOURS:g}h "
            f"airport check-in buffer ({check_in_by.astimezone(LAGOS).strftime('%I:%M %p').lstrip('0')})."
        )
        log_event(logger, logging.INFO, "shuttle match fit", trip=str(best.id), city=city)
        return ShuttleSuggestion(
            trip=to_trip_out(best, best.route),
            fits=True,
            check_in_by=check_in_iso,
            message=msg,
            alternatives=alts,
        )

    # No trip arrives before check-in — pick nearest earlier option (closest arrival
    # before the flight, or overall nearest if none).
    before_flight = [t for t in airport_trips if _trip_arrival(t) < flight_dep]
    pool = before_flight or airport_trips
    if not pool:
        return ShuttleSuggestion(
            trip=None,
            fits=False,
            check_in_by=check_in_iso,
            message=(
                f"No shuttles from {city} toward Sam Mbakwe Airport were found for that day. "
                "Try another pickup city or enter a different flight time."
            ),
            alternatives=[],
        )

    def _distance(t: Trip) -> timedelta:
        arrival = _trip_arrival(t)
        return abs(arrival - check_in_by)

    best = min(pool, key=_distance)
    # Prefer the latest before-flight trip when distances tie-ish.
    if before_flight:
        best = before_flight[-1]

    alts = [
        to_trip_out(t, t.route)
        for t in reversed([t for t in pool if t.id != best.id])
    ][:limit_alternatives]
    arrival_local = _trip_arrival(best).astimezone(LAGOS).strftime("%I:%M %p").lstrip("0")
    check_local = check_in_by.astimezone(LAGOS).strftime("%I:%M %p").lstrip("0")
    msg = (
        f"No shuttle arrives before the recommended check-in time ({check_local}). "
        f"Closest option arrives around {arrival_local} — you may be tight for check-in."
    )
    log_event(logger, logging.INFO, "shuttle match no fit", trip=str(best.id), city=city)
    return ShuttleSuggestion(
        trip=to_trip_out(best, best.route),
        fits=False,
        check_in_by=check_in_iso,
        message=msg,
        alternatives=alts,
    )
