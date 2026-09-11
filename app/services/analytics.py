"""Operations analytics — revenue, occupancy, channel mix, route performance."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import Date, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import LAGOS, now_utc, today_lagos
from app.models.booking import Booking
from app.models.enums import BookingStatus, SubscriptionStatus, TripStatus
from app.models.route import Route
from app.models.subscription import Subscription, SubscriptionPlan
from app.models.trip import Trip
from app.schemas.analytics import (
    AnalyticsOverview,
    ChannelBreakdown,
    DashboardSummary,
    OccupancySlot,
    OverviewTrip,
    RevenuePoint,
    RoutePerformance,
    SubscriptionSales,
    TripAlert,
)

#: Bookings that count as revenue.
EARNING_STATUSES = [BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN, BookingStatus.COMPLETED]


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time()).replace(tzinfo=LAGOS)
    return start, start + timedelta(days=1)


async def _revenue_between(db: AsyncSession, start: datetime, end: datetime) -> tuple[int, int, int]:
    """(revenue_kobo, bookings, seats) for bookings created in a window."""
    stmt = select(
        func.coalesce(func.sum(Booking.amount_kobo), 0),
        func.count(Booking.id),
        func.coalesce(func.sum(Booking.seats), 0),
    ).where(
        and_(
            Booking.created_at >= start,
            Booking.created_at < end,
            Booking.status.in_(EARNING_STATUSES),
        )
    )
    row = (await db.execute(stmt)).one()
    return int(row[0]), int(row[1]), int(row[2])


async def channel_breakdown(db: AsyncSession, start: datetime, end: datetime) -> list[ChannelBreakdown]:
    stmt = (
        select(
            Booking.source,
            func.count(Booking.id),
            func.coalesce(func.sum(Booking.seats), 0),
            func.coalesce(func.sum(Booking.amount_kobo), 0),
        )
        .where(
            and_(
                Booking.created_at >= start,
                Booking.created_at < end,
                Booking.status.in_(EARNING_STATUSES),
            )
        )
        .group_by(Booking.source)
        .order_by(func.count(Booking.id).desc())
    )
    rows = (await db.execute(stmt)).all()
    total = sum(r[1] for r in rows) or 1
    return [
        ChannelBreakdown(
            source=r[0],
            bookings=int(r[1]),
            seats=int(r[2]),
            revenue_kobo=int(r[3]),
            share_pct=round(r[1] * 100 / total, 1),
        )
        for r in rows
    ]


async def dashboard_summary(db: AsyncSession) -> DashboardSummary:
    today = today_lagos()
    day_start, day_end = _day_bounds(today)
    week_start = day_start - timedelta(days=today.weekday())
    month_start = datetime(today.year, today.month, 1, tzinfo=LAGOS)

    rev_today, bookings_today, _ = await _revenue_between(db, day_start, day_end)
    rev_week, bookings_week, _ = await _revenue_between(db, week_start, day_end)
    rev_month, bookings_month, _ = await _revenue_between(db, month_start, day_end)

    seat_stmt = select(
        func.count(Trip.id),
        func.coalesce(func.sum(Trip.seats_total), 0),
        func.coalesce(func.sum(Trip.seats_booked), 0),
    ).where(and_(Trip.service_date == today, Trip.status != TripStatus.CANCELLED))
    trips_today, seats_offered, seats_sold = (await db.execute(seat_stmt)).one()

    remaining = (
        await db.execute(
            select(func.count(Trip.id)).where(
                and_(
                    Trip.service_date == today,
                    Trip.departure_datetime > now_utc(),
                    Trip.status.in_([TripStatus.SCHEDULED, TripStatus.BOARDING]),
                )
            )
        )
    ).scalar_one()

    active_subs = (
        await db.execute(
            select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.ACTIVE)
        )
    ).scalar_one()

    sub_revenue = (
        await db.execute(
            select(func.coalesce(func.sum(Subscription.amount_paid_kobo), 0)).where(
                and_(
                    Subscription.created_at >= month_start,
                    Subscription.status.in_(
                        [SubscriptionStatus.ACTIVE, SubscriptionStatus.EXHAUSTED, SubscriptionStatus.EXPIRED]
                    ),
                )
            )
        )
    ).scalar_one()

    total_month = (
        await db.execute(
            select(func.count(Booking.id)).where(Booking.created_at >= month_start)
        )
    ).scalar_one()
    cancelled_month = (
        await db.execute(
            select(func.count(Booking.id)).where(
                and_(Booking.created_at >= month_start, Booking.status == BookingStatus.CANCELLED)
            )
        )
    ).scalar_one()

    return DashboardSummary(
        date=today,
        trips_today=int(trips_today),
        departures_remaining=int(remaining),
        seats_offered_today=int(seats_offered),
        seats_sold_today=int(seats_sold),
        occupancy_today_pct=round(seats_sold * 100 / seats_offered, 1) if seats_offered else 0.0,
        revenue_today_kobo=rev_today,
        revenue_week_kobo=rev_week,
        revenue_month_kobo=rev_month,
        bookings_today=bookings_today,
        bookings_week=bookings_week,
        bookings_month=bookings_month,
        active_subscriptions=int(active_subs),
        subscription_revenue_month_kobo=int(sub_revenue),
        cancellation_rate_pct=round(cancelled_month * 100 / total_month, 1) if total_month else 0.0,
        channel_breakdown=await channel_breakdown(db, month_start, day_end),
    )


def _overview_trip(trip: Trip) -> OverviewTrip:
    driver_name = trip.driver.user.full_name if trip.driver and trip.driver.user else None
    return OverviewTrip(
        trip_id=trip.id,
        route_name=trip.route.name if trip.route else "—",
        departure_datetime=trip.departure_datetime.astimezone(LAGOS).isoformat(),
        status=trip.status,
        seats_total=trip.seats_total,
        seats_booked=trip.seats_booked,
        occupancy_pct=round(trip.seats_booked * 100 / trip.seats_total, 1) if trip.seats_total else 0.0,
        driver_name=driver_name,
        vehicle_name=trip.vehicle.name if trip.vehicle else None,
    )


async def overview(db: AsyncSession) -> AnalyticsOverview:
    today = today_lagos()
    todays = list(
        (
            await db.execute(
                select(Trip).where(Trip.service_date == today).order_by(Trip.departure_datetime.asc())
            )
        ).unique().scalars().all()
    )
    upcoming = list(
        (
            await db.execute(
                select(Trip)
                .where(
                    and_(
                        Trip.departure_datetime > now_utc(),
                        Trip.status.in_([TripStatus.SCHEDULED, TripStatus.BOARDING]),
                    )
                )
                .order_by(Trip.departure_datetime.asc())
                .limit(8)
            )
        ).unique().scalars().all()
    )

    alerts: list[TripAlert] = []
    soon = now_utc() + timedelta(hours=24)
    for trip in upcoming:
        local = trip.departure_datetime.astimezone(LAGOS).isoformat()
        if trip.driver_id is None:
            alerts.append(
                TripAlert(
                    trip_id=trip.id,
                    severity="high",
                    kind="unassigned_driver",
                    message=f"No driver assigned to the {trip.departure_datetime.astimezone(LAGOS):%H:%M} "
                    f"{trip.route.name if trip.route else ''} departure.",
                    departure_datetime=local,
                )
            )
        if trip.vehicle_id is None:
            alerts.append(
                TripAlert(
                    trip_id=trip.id,
                    severity="high",
                    kind="unassigned_vehicle",
                    message=f"No vehicle assigned to the {trip.departure_datetime.astimezone(LAGOS):%H:%M} departure.",
                    departure_datetime=local,
                )
            )
        occupancy = trip.seats_booked / trip.seats_total if trip.seats_total else 0
        if trip.departure_datetime <= soon and occupancy < 0.3:
            alerts.append(
                TripAlert(
                    trip_id=trip.id,
                    severity="medium",
                    kind="low_occupancy",
                    message=f"Only {trip.seats_booked}/{trip.seats_total} seats sold on the "
                    f"{trip.departure_datetime.astimezone(LAGOS):%H:%M} departure within 24 hours.",
                    departure_datetime=local,
                )
            )

    return AnalyticsOverview(
        summary=await dashboard_summary(db),
        todays_trips=[_overview_trip(t) for t in todays],
        upcoming_departures=[_overview_trip(t) for t in upcoming],
        alerts=alerts[:12],
    )


async def revenue_series(
    db: AsyncSession, *, start: date, end: date, granularity: str = "day"
) -> list[RevenuePoint]:
    trunc = {"day": "day", "week": "week", "month": "month"}.get(granularity, "day")
    bucket = func.date_trunc(trunc, func.timezone("Africa/Lagos", Booking.created_at))
    start_dt, _ = _day_bounds(start)
    _, end_dt = _day_bounds(end)

    stmt = (
        select(
            bucket.label("bucket"),
            func.coalesce(func.sum(Booking.amount_kobo), 0),
            func.count(Booking.id),
            func.coalesce(func.sum(Booking.seats), 0),
        )
        .where(
            and_(
                Booking.created_at >= start_dt,
                Booking.created_at < end_dt,
                Booking.status.in_(EARNING_STATUSES),
            )
        )
        .group_by("bucket")
        .order_by("bucket")
    )
    rows = (await db.execute(stmt)).all()
    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%V", "month": "%Y-%m"}[trunc]
    return [
        RevenuePoint(
            period=r[0].strftime(fmt),
            revenue_kobo=int(r[1]),
            bookings=int(r[2]),
            seats=int(r[3]),
        )
        for r in rows
    ]


async def route_performance(db: AsyncSession, *, start: date, end: date) -> list[RoutePerformance]:
    stmt = (
        select(
            Route.id,
            Route.name,
            func.count(Trip.id),
            func.coalesce(func.sum(Trip.seats_total), 0),
            func.coalesce(func.sum(Trip.seats_booked), 0),
        )
        .join(Trip, Trip.route_id == Route.id)
        .where(
            and_(
                Trip.service_date >= start,
                Trip.service_date <= end,
                Trip.status != TripStatus.CANCELLED,
            )
        )
        .group_by(Route.id, Route.name)
        .order_by(func.coalesce(func.sum(Trip.seats_booked), 0).desc())
    )
    rows = (await db.execute(stmt)).all()

    revenue_stmt = (
        select(Trip.route_id, func.coalesce(func.sum(Booking.amount_kobo), 0))
        .join(Booking, Booking.trip_id == Trip.id)
        .where(
            and_(
                Trip.service_date >= start,
                Trip.service_date <= end,
                Booking.status.in_(EARNING_STATUSES),
            )
        )
        .group_by(Trip.route_id)
    )
    revenue = {r[0]: int(r[1]) for r in (await db.execute(revenue_stmt)).all()}

    return [
        RoutePerformance(
            route_id=r[0],
            route_name=r[1],
            trips=int(r[2]),
            seats_offered=int(r[3]),
            seats_sold=int(r[4]),
            occupancy_pct=round(r[4] * 100 / r[3], 1) if r[3] else 0.0,
            revenue_kobo=revenue.get(r[0], 0),
        )
        for r in rows
    ]


async def occupancy_by_time_slot(db: AsyncSession, *, start: date, end: date) -> list[OccupancySlot]:
    slot = func.to_char(func.timezone("Africa/Lagos", Trip.departure_datetime), "HH24:MI")
    stmt = (
        select(
            slot.label("slot"),
            func.count(Trip.id),
            func.coalesce(func.sum(Trip.seats_total), 0),
            func.coalesce(func.sum(Trip.seats_booked), 0),
        )
        .where(
            and_(
                Trip.service_date >= start,
                Trip.service_date <= end,
                Trip.status != TripStatus.CANCELLED,
            )
        )
        .group_by("slot")
        .order_by("slot")
    )
    rows = (await db.execute(stmt)).all()
    return [
        OccupancySlot(
            time_slot=r[0],
            trips=int(r[1]),
            seats_offered=int(r[2]),
            seats_sold=int(r[3]),
            occupancy_pct=round(r[3] * 100 / r[2], 1) if r[2] else 0.0,
        )
        for r in rows
    ]


async def subscription_sales(db: AsyncSession, *, start: date, end: date) -> list[SubscriptionSales]:
    start_dt, _ = _day_bounds(start)
    _, end_dt = _day_bounds(end)
    stmt = (
        select(
            SubscriptionPlan.id,
            SubscriptionPlan.name,
            func.count(Subscription.id),
            func.coalesce(func.sum(Subscription.amount_paid_kobo), 0),
            func.coalesce(
                func.sum(case((Subscription.status == SubscriptionStatus.ACTIVE, 1), else_=0)), 0
            ),
        )
        .join(Subscription, Subscription.plan_id == SubscriptionPlan.id, isouter=True)
        .where(
            and_(
                Subscription.created_at >= start_dt,
                Subscription.created_at < end_dt,
                Subscription.status != SubscriptionStatus.PENDING_PAYMENT,
            )
        )
        .group_by(SubscriptionPlan.id, SubscriptionPlan.name)
        .order_by(func.coalesce(func.sum(Subscription.amount_paid_kobo), 0).desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        SubscriptionSales(
            plan_id=r[0], plan_name=r[1], sold=int(r[2]), revenue_kobo=int(r[3]), active=int(r[4])
        )
        for r in rows
    ]


async def bookings_export_rows(db: AsyncSession, *, start: date, end: date) -> list[dict]:
    start_dt, _ = _day_bounds(start)
    _, end_dt = _day_bounds(end)
    stmt = (
        select(Booking, Trip, Route)
        .join(Trip, Trip.id == Booking.trip_id)
        .join(Route, Route.id == Trip.route_id)
        .where(and_(Booking.created_at >= start_dt, Booking.created_at < end_dt))
        .order_by(Booking.created_at.desc())
    )
    rows = (await db.execute(stmt)).unique().all()
    return [
        {
            "booking_ref": b.booking_ref,
            "created_at": b.created_at.astimezone(LAGOS).strftime("%Y-%m-%d %H:%M"),
            "passenger_name": b.passenger_name,
            "passenger_phone": b.passenger_phone,
            "passenger_email": b.passenger_email or "",
            "route": r.name,
            "service_date": t.service_date.isoformat(),
            "departure": t.departure_datetime.astimezone(LAGOS).strftime("%H:%M"),
            "seats": b.seats,
            "seats_male": b.seats_male,
            "seats_female": b.seats_female,
            "seat_numbers": " ".join(b.seat_numbers or []),
            "amount_naira": f"{b.amount_kobo / 100:.2f}",
            "source": b.source,
            "status": b.status,
        }
        for b, t, r in rows
    ]


async def route_ids_for_names(db: AsyncSession) -> dict[uuid.UUID, str]:
    rows = (await db.execute(select(Route.id, Route.name))).all()
    return {r[0]: r[1] for r in rows}


async def demographics(db: AsyncSession, *, start: date, end: date) -> dict:
    """Passenger sex split for state reporting.

    Bookings taken before demographics were collected sit at 0/0 and are counted
    as `unspecified` rather than being silently dropped from the denominator —
    otherwise the percentages would overstate coverage.
    """
    start_dt, _ = _day_bounds(start)
    _, end_dt = _day_bounds(end)

    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(Booking.seats_male), 0),
                func.coalesce(func.sum(Booking.seats_female), 0),
                func.coalesce(func.sum(Booking.seats), 0),
                func.count(Booking.id),
                func.coalesce(
                    func.sum(
                        case(
                            ((Booking.seats_male == 0) & (Booking.seats_female == 0), Booking.seats),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(
                and_(
                    Booking.created_at >= start_dt,
                    Booking.created_at < end_dt,
                    Booking.status.in_(EARNING_STATUSES),
                )
            )
        )
    ).one()

    male, female, seats, bookings, unspecified = (int(v) for v in row)
    recorded = male + female

    return {
        "male": male,
        "female": female,
        "unspecified": unspecified,
        "total_seats": seats,
        "bookings": bookings,
        "male_pct": round(male * 100 / recorded, 1) if recorded else 0.0,
        "female_pct": round(female * 100 / recorded, 1) if recorded else 0.0,
        "coverage_pct": round(recorded * 100 / seats, 1) if seats else 0.0,
    }


async def demographics_by_route(db: AsyncSession, *, start: date, end: date) -> list[dict]:
    start_dt, _ = _day_bounds(start)
    _, end_dt = _day_bounds(end)

    stmt = (
        select(
            Route.name,
            func.coalesce(func.sum(Booking.seats_male), 0),
            func.coalesce(func.sum(Booking.seats_female), 0),
        )
        .join(Trip, Trip.id == Booking.trip_id)
        .join(Route, Route.id == Trip.route_id)
        .where(
            and_(
                Booking.created_at >= start_dt,
                Booking.created_at < end_dt,
                Booking.status.in_(EARNING_STATUSES),
            )
        )
        .group_by(Route.name)
        .order_by(Route.name)
    )
    return [
        {"route_name": r[0], "male": int(r[1]), "female": int(r[2])}
        for r in (await db.execute(stmt)).all()
    ]
