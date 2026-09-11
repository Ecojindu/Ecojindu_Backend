"""Trip availability, timetable materialisation and seat accounting."""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotFoundError
from app.core.logging import log_event
from app.core.timeutil import combine_lagos, now_utc, today_lagos
from app.models.booking import Booking
from app.models.enums import BookingStatus, TripStatus
from app.models.route import Route
from app.models.trip import Trip, TripTemplate
from app.schemas.trip import TripOut

logger = logging.getLogger("ecojindu.trips")


def to_trip_out(trip: Trip, route: Route | None = None) -> TripOut:
    """Serialise a Trip. Pass `route` only when it is already in hand."""
    out = TripOut.model_validate(trip)
    if route is not None:
        out.route_name = route.name
        out.origin_terminal = route.origin_terminal
        out.destination = route.destination
        out.duration_mins = route.duration_mins
    return out


async def get_trip_or_404(db: AsyncSession, trip_id: uuid.UUID) -> Trip:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise NotFoundError("That departure could not be found.")
    return trip


async def lock_trip(db: AsyncSession, trip_id: uuid.UUID) -> Trip:
    """Fetch a trip under a row-level write lock.

    This is what makes concurrent seat reservations safe: two requests racing
    for the last seat serialise here, so the second sees the first's count.

    The lock is taken with a column-only SELECT because Postgres refuses
    `FOR UPDATE` on the nullable side of an outer join, and the Trip mapper
    eager-joins route/vehicle/driver. The ORM object is then loaded normally —
    the row lock is already held for the rest of the transaction.
    """
    locked = (
        await db.execute(select(Trip.id).where(Trip.id == trip_id).with_for_update())
    ).scalar_one_or_none()
    if locked is None:
        raise NotFoundError("That departure could not be found.")

    trip = await db.get(Trip, trip_id, populate_existing=True)
    if trip is None:
        raise NotFoundError("That departure could not be found.")
    return trip


async def search_trips(
    db: AsyncSession,
    *,
    route_id: uuid.UUID | None = None,
    service_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    origin: str | None = None,
    destination: str | None = None,
    min_seats: int = 1,
    include_past: bool = False,
    include_cancelled: bool = False,
    limit: int = 200,
) -> list[Trip]:
    stmt = select(Trip).join(Route, Route.id == Trip.route_id)

    if route_id:
        stmt = stmt.where(Trip.route_id == route_id)
    if service_date:
        stmt = stmt.where(Trip.service_date == service_date)
    if date_from:
        stmt = stmt.where(Trip.service_date >= date_from)
    if date_to:
        stmt = stmt.where(Trip.service_date <= date_to)
    if origin:
        stmt = stmt.where(Route.origin_terminal.ilike(f"%{origin}%"))
    if destination:
        stmt = stmt.where(Route.destination.ilike(f"%{destination}%"))
    if not include_cancelled:
        stmt = stmt.where(Trip.status != TripStatus.CANCELLED)
    if not include_past:
        stmt = stmt.where(Trip.departure_datetime >= now_utc())
    if min_seats > 1:
        stmt = stmt.where(Trip.seats_total - Trip.seats_booked >= min_seats)

    stmt = stmt.order_by(Trip.departure_datetime.asc()).limit(limit)
    return list((await db.execute(stmt)).unique().scalars().all())


async def release_expired_holds(db: AsyncSession) -> int:
    """Return seats from payment holds that were never completed.

    Runs on a 60-second timer and opportunistically before each availability
    read, so the site never shows a seat as taken by an abandoned checkout.
    """
    now = now_utc()
    stmt = select(Booking).where(
        and_(
            Booking.status == BookingStatus.PENDING_PAYMENT,
            Booking.hold_expires_at.is_not(None),
            Booking.hold_expires_at < now,
        )
    )
    expired = list((await db.execute(stmt)).unique().scalars().all())
    if not expired:
        return 0

    released = 0
    for booking in expired:
        trip = await lock_trip(db, booking.trip_id)
        trip.seats_booked = max(trip.seats_booked - booking.seats, 0)
        booking.status = BookingStatus.CANCELLED
        booking.cancelled_at = now
        booking.cancellation_reason = "Payment not completed within the hold window"
        released += booking.seats

    await db.flush()
    log_event(logger, logging.INFO, "released expired seat holds", bookings=len(expired), seats=released)
    return released


def _next_seat_numbers(taken: set[str], count: int, capacity: int) -> list[str]:
    """Assign the lowest free seat numbers, 1-indexed, as strings."""
    seats: list[str] = []
    for n in range(1, capacity + 1):
        if len(seats) == count:
            break
        label = str(n)
        if label not in taken:
            seats.append(label)
    return seats


async def allocate_seat_numbers(db: AsyncSession, trip: Trip, count: int) -> list[str]:
    stmt = select(Booking.seat_numbers).where(
        and_(
            Booking.trip_id == trip.id,
            Booking.status.in_(
                [
                    BookingStatus.PENDING_PAYMENT,
                    BookingStatus.CONFIRMED,
                    BookingStatus.CHECKED_IN,
                    BookingStatus.COMPLETED,
                ]
            ),
        )
    )
    taken: set[str] = set()
    for row in (await db.execute(stmt)).scalars().all():
        taken.update(row or [])
    return _next_seat_numbers(taken, count, trip.seats_total)


async def generate_trips_from_templates(
    db: AsyncSession, *, days_ahead: int | None = None, start: date | None = None
) -> int:
    """Materialise `trips` rows from active timetable templates.

    Idempotent: the (template_id, service_date) unique constraint means running
    it twice creates nothing extra, so the nightly job is safe to retry.
    """
    days_ahead = days_ahead or settings.TRIP_GENERATION_DAYS_AHEAD
    start = start or today_lagos()

    templates = list(
        (
            await db.execute(
                select(TripTemplate).where(TripTemplate.is_active.is_(True))
            )
        ).unique().scalars().all()
    )
    if not templates:
        return 0

    horizon = [start + timedelta(days=i) for i in range(days_ahead)]

    existing_stmt = select(Trip.template_id, Trip.service_date).where(
        and_(
            Trip.template_id.is_not(None),
            Trip.service_date >= start,
            Trip.service_date <= horizon[-1],
        )
    )
    existing = {(tid, d) for tid, d in (await db.execute(existing_stmt)).all()}

    created = 0
    for template in templates:
        route = template.route
        capacity = template.vehicle.seat_capacity if template.vehicle else 14
        fare = template.fare_override_kobo or route.base_fare_kobo

        for day in horizon:
            if day.isoweekday() not in (template.days_of_week or []):
                continue
            if (template.id, day) in existing:
                continue

            departure = combine_lagos(day, template.departure_time)
            if departure <= now_utc():
                continue

            db.add(
                Trip(
                    template_id=template.id,
                    route_id=template.route_id,
                    service_date=day,
                    departure_datetime=departure,
                    arrival_estimate=departure + timedelta(minutes=route.duration_mins),
                    vehicle_id=template.vehicle_id,
                    driver_id=template.driver_id,
                    status=TripStatus.SCHEDULED,
                    seats_total=capacity,
                    seats_booked=0,
                    fare_kobo=fare,
                )
            )
            created += 1

    await db.flush()
    log_event(logger, logging.INFO, "trip generation complete", created=created, days_ahead=days_ahead)
    return created


async def trip_revenue_map(db: AsyncSession, trip_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    """{trip_id: (confirmed_booking_count, revenue_kobo)} for a set of trips."""
    if not trip_ids:
        return {}
    stmt = (
        select(
            Booking.trip_id,
            func.count(Booking.id),
            func.coalesce(func.sum(Booking.amount_kobo), 0),
        )
        .where(
            and_(
                Booking.trip_id.in_(trip_ids),
                Booking.status.in_(
                    [BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN, BookingStatus.COMPLETED]
                ),
            )
        )
        .group_by(Booking.trip_id)
    )
    return {row[0]: (row[1], row[2]) for row in (await db.execute(stmt)).all()}


async def complete_finished_trips(db: AsyncSession) -> int:
    """Mark arrived trips' bookings as completed once the journey window closes."""
    cutoff = now_utc() - timedelta(hours=6)
    stmt = select(Trip).where(
        and_(Trip.departure_datetime < cutoff, Trip.status.in_([TripStatus.DEPARTED, TripStatus.ARRIVED]))
    )
    trips = list((await db.execute(stmt)).unique().scalars().all())
    updated = 0
    for trip in trips:
        if trip.status != TripStatus.ARRIVED:
            trip.status = TripStatus.ARRIVED
        bookings = list(
            (
                await db.execute(
                    select(Booking).where(
                        and_(
                            Booking.trip_id == trip.id,
                            Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN]),
                        )
                    )
                )
            ).unique().scalars().all()
        )
        for booking in bookings:
            booking.status = BookingStatus.COMPLETED
            updated += 1
    await db.flush()
    return updated


def departure_window(trip: Trip) -> tuple[datetime, datetime]:
    return trip.departure_datetime, trip.arrival_estimate or trip.departure_datetime
