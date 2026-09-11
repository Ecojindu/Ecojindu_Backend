"""Driver-scoped endpoints for the phone-friendly driver portal."""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, select

from app.api.deps import CurrentDriver, DbSession, DriverUser
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.core.timeutil import now_utc, today_lagos
from app.models.enums import TripStatus
from app.models.route import Route
from app.models.trip import Trip
from app.schemas.common import Message
from app.schemas.trip import TripManifest, TripOut, TripStatusUpdate
from app.services import audit as audit_service
from app.services import bookings as booking_service
from app.services import notifications as notification_service
from app.services.trips import to_trip_out

router = APIRouter(prefix="/driver", tags=["Driver portal"])

#: Allowed forward transitions — a driver can advance a trip but never rewind it.
STATUS_FLOW = {
    TripStatus.SCHEDULED: {TripStatus.BOARDING, TripStatus.DEPARTED},
    TripStatus.BOARDING: {TripStatus.DEPARTED},
    TripStatus.DEPARTED: {TripStatus.ARRIVED},
    TripStatus.ARRIVED: set(),
    TripStatus.CANCELLED: set(),
}


@router.get("/trips", response_model=list[TripOut], summary="My assigned trips")
async def my_trips(
    db: DbSession,
    user: DriverUser,
    driver: CurrentDriver,
    days: int = Query(7, ge=1, le=30, description="How far ahead to look"),
    include_past: bool = Query(False, description="Include earlier trips from today"),
) -> list[TripOut]:
    today = today_lagos()
    stmt = (
        select(Trip)
        .where(
            and_(
                Trip.driver_id == driver.id,
                Trip.service_date >= today,
                Trip.service_date <= today + timedelta(days=days),
            )
        )
        .order_by(Trip.departure_datetime.asc())
    )
    trips = list((await db.execute(stmt)).unique().scalars().all())
    if not include_past:
        trips = [t for t in trips if t.departure_datetime >= now_utc() - timedelta(hours=4)]
    return [to_trip_out(t) for t in trips]


@router.get("/trips/today", response_model=list[TripOut], summary="Today's runs")
async def todays_trips(db: DbSession, user: DriverUser, driver: CurrentDriver) -> list[TripOut]:
    stmt = (
        select(Trip)
        .where(and_(Trip.driver_id == driver.id, Trip.service_date == today_lagos()))
        .order_by(Trip.departure_datetime.asc())
    )
    trips = list((await db.execute(stmt)).unique().scalars().all())
    return [to_trip_out(t) for t in trips]


async def _assert_assigned(db, trip_id: uuid.UUID, driver_id: uuid.UUID) -> Trip:
    trip = await db.get(Trip, trip_id)
    if trip is None:
        raise NotFoundError("That trip could not be found.")
    if trip.driver_id != driver_id:
        raise ForbiddenError("You are not assigned to that trip.")
    return trip


@router.get(
    "/trips/{trip_id}/manifest",
    response_model=TripManifest,
    summary="Passenger manifest with seat numbers and pickup points",
)
async def driver_manifest(
    trip_id: uuid.UUID, db: DbSession, user: DriverUser, driver: CurrentDriver
) -> TripManifest:
    from app.api.v1.admin import build_manifest

    await _assert_assigned(db, trip_id, driver.id)
    return await build_manifest(db, trip_id)


@router.post(
    "/trips/{trip_id}/status",
    response_model=TripOut,
    summary="Advance a trip: Boarding → Departed → Arrived",
)
async def update_trip_status(
    trip_id: uuid.UUID,
    payload: TripStatusUpdate,
    db: DbSession,
    user: DriverUser,
    driver: CurrentDriver,
    request: Request,
) -> TripOut:
    trip = await _assert_assigned(db, trip_id, driver.id)

    target = payload.status
    if target not in {str(s) for s in TripStatus}:
        raise ValidationError(f"`{target}` is not a valid trip status.")
    if target == TripStatus.CANCELLED:
        raise ForbiddenError("Only operations can cancel a departure.")
    if target not in {str(s) for s in STATUS_FLOW.get(TripStatus(trip.status), set())}:
        raise ValidationError(f"A trip that is `{trip.status}` cannot move to `{target}`.")

    trip.status = target

    if payload.notify_passengers:
        route = await db.get(Route, trip.route_id)
        note = {
            TripStatus.BOARDING: "Boarding has started — please make your way to the shuttle.",
            TripStatus.DEPARTED: "Your shuttle has departed.",
            TripStatus.ARRIVED: "Your shuttle has arrived. Thank you for riding green with us.",
        }.get(TripStatus(target), payload.reason or "Trip status updated.")
        for booking in await booking_service.bookings_for_trip(db, trip.id):
            await notification_service.send_schedule_change(db, booking, trip, route, note)

    await audit_service.record(
        db, actor=user, action="trip.status", entity_type="trip",
        entity_id=trip.id, entity_label=trip.route.name if trip.route else None, request=request,
        summary=(
            f"Marked the {trip.departure_datetime:%H:%M} "
            f"{trip.route.name if trip.route else ''} departure as {target}"
            + (" and notified the passengers." if payload.notify_passengers else ".")
        ),
    )
    await db.commit()
    await db.refresh(trip)
    return to_trip_out(trip)


@router.get("/me", response_model=dict, summary="My driver profile")
async def driver_profile(user: DriverUser, driver: CurrentDriver) -> dict:
    return {
        "driver_id": str(driver.id),
        "full_name": user.full_name,
        "phone": user.phone,
        "email": user.email,
        "license_no": driver.license_no,
        "status": driver.status,
        "photo_url": driver.photo_url,
        "assigned_vehicle": (
            {
                "id": str(driver.assigned_vehicle.id),
                "name": driver.assigned_vehicle.name,
                "plate_no": driver.assigned_vehicle.plate_no,
                "model": driver.assigned_vehicle.model,
                "seat_capacity": driver.assigned_vehicle.seat_capacity,
                "range_km": driver.assigned_vehicle.range_km,
            }
            if driver.assigned_vehicle
            else None
        ),
    }


@router.get("/summary", response_model=dict, summary="Today at a glance")
async def driver_summary(db: DbSession, user: DriverUser, driver: CurrentDriver) -> dict:
    stmt = select(Trip).where(
        and_(Trip.driver_id == driver.id, Trip.service_date == today_lagos())
    )
    trips = list((await db.execute(stmt)).unique().scalars().all())
    return {
        "date": today_lagos().isoformat(),
        "trips_today": len(trips),
        "passengers_today": sum(t.seats_booked for t in trips),
        "completed": sum(1 for t in trips if t.status == TripStatus.ARRIVED),
        "next_departure": min(
            (t.departure_datetime.isoformat() for t in trips if t.departure_datetime > now_utc()),
            default=None,
        ),
    }


@router.get("/manifest-summary/{trip_id}", response_model=Message, summary="Quick headcount")
async def manifest_summary(
    trip_id: uuid.UUID, db: DbSession, user: DriverUser, driver: CurrentDriver
) -> Message:
    trip = await _assert_assigned(db, trip_id, driver.id)
    rows = await booking_service.bookings_for_trip(db, trip_id)
    return Message(
        message=f"{sum(b.seats for b in rows)} passenger(s) booked on {trip.seats_total} seats."
    )
