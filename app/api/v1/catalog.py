"""Public read-only catalogue: routes, stops, plans and bookable departures."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DbSession
from app.core.errors import NotFoundError
from app.core.timeutil import today_lagos
from app.models.route import Route
from app.models.subscription import SubscriptionPlan
from app.schemas.catalog import RouteOut
from app.schemas.subscription import PlanOut
from app.schemas.trip import TripOut
from app.services.trips import release_expired_holds, search_trips, to_trip_out

router = APIRouter(tags=["Catalogue"])


@router.get("/routes", response_model=list[RouteOut], summary="All active routes with their stops")
async def list_routes(db: DbSession, include_inactive: bool = False) -> list[RouteOut]:
    stmt = select(Route).order_by(Route.name)
    if not include_inactive:
        stmt = stmt.where(Route.is_active.is_(True))
    routes = list((await db.execute(stmt)).unique().scalars().all())
    return [RouteOut.model_validate(r) for r in routes]


@router.get("/routes/{route_id}", response_model=RouteOut, summary="A single route")
async def get_route(route_id: uuid.UUID, db: DbSession) -> RouteOut:
    route = await db.get(Route, route_id)
    if route is None:
        raise NotFoundError("That route could not be found.")
    return RouteOut.model_validate(route)


@router.get(
    "/trips",
    response_model=list[TripOut],
    summary="Search bookable departures with live seat availability",
)
async def list_trips(
    db: DbSession,
    route_id: uuid.UUID | None = None,
    service_date: date | None = Query(None, description="Single service date (YYYY-MM-DD)"),
    date_from: date | None = None,
    date_to: date | None = None,
    origin: str | None = Query(None, description="Partial match on the origin terminal"),
    destination: str | None = Query(None, description="Partial match on the destination"),
    seats: int = Query(1, ge=1, le=14, description="Only return departures with at least this many seats"),
    include_past: bool = False,
) -> list[TripOut]:
    # Return abandoned checkouts' seats to the pool before reporting availability.
    await release_expired_holds(db)
    await db.commit()

    trips = await search_trips(
        db,
        route_id=route_id,
        service_date=service_date,
        date_from=date_from,
        date_to=date_to,
        origin=origin,
        destination=destination,
        min_seats=seats,
        include_past=include_past,
    )
    return [to_trip_out(t) for t in trips]


@router.get(
    "/trips/availability",
    response_model=dict[str, int],
    summary="Seats left per day across a date window (for the date switcher)",
)
async def availability_calendar(
    db: DbSession,
    route_id: uuid.UUID,
    days: int = Query(14, ge=1, le=60),
) -> dict[str, int]:
    start = today_lagos()
    trips = await search_trips(
        db, route_id=route_id, date_from=start, date_to=start + timedelta(days=days - 1)
    )
    calendar: dict[str, int] = {
        (start + timedelta(days=i)).isoformat(): 0 for i in range(days)
    }
    for trip in trips:
        key = trip.service_date.isoformat()
        if key in calendar:
            calendar[key] += trip.seats_available
    return calendar


@router.get("/trips/{trip_id}", response_model=TripOut, summary="A single departure")
async def get_trip(trip_id: uuid.UUID, db: DbSession) -> TripOut:
    from app.services.trips import get_trip_or_404

    trip = await get_trip_or_404(db, trip_id)
    return to_trip_out(trip)


@router.get("/plans", response_model=list[PlanOut], summary="Subscription tiers")
async def list_plans(db: DbSession, include_inactive: bool = False) -> list[PlanOut]:
    stmt = select(SubscriptionPlan).order_by(SubscriptionPlan.sort_order, SubscriptionPlan.price_kobo)
    if not include_inactive:
        stmt = stmt.where(SubscriptionPlan.is_active.is_(True))
    plans = list((await db.execute(stmt)).scalars().all())
    return [PlanOut.model_validate(p) for p in plans]
