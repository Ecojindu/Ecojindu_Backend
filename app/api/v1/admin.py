"""Operations & super-admin API: catalogue CRUD, timetable, trips, bookings, people."""
from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query, Request, Response, status
from sqlalchemy import String, and_, case, cast, desc, func, or_, select

from app.api.deps import AdminUser, DbSession, SuperAdminUser
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import hash_password
from app.core.timeutil import LAGOS, fmt_datetime, naira, now_utc, today_lagos
from app.models.audit import AuditLog
from app.models.booking import Booking, Ticket
from app.models.enums import BookingSource, BookingStatus, TripStatus, UserRole
from app.models.fleet import Vehicle
from app.models.notification import Notification, NotificationTemplate
from app.models.route import Route, RouteStop
from app.models.subscription import Subscription, SubscriptionPlan
from app.models.trip import Trip, TripTemplate
from app.models.user import Driver, User
from app.schemas.auth import UserOut
from app.schemas.booking import AdminBookingCreate, BookingOut
from app.schemas.catalog import (
    DriverIn,
    RouteAdminOut,
    DriverOut,
    DriverUpdate,
    RouteIn,
    RouteOut,
    RouteStopIn,
    RouteStopOut,
    RouteUpdate,
    TripTemplateIn,
    TripTemplateOut,
    TripTemplateUpdate,
    VehicleIn,
    VehicleOut,
    VehicleUpdate,
)
from app.schemas.common import Message, Page
from app.schemas.subscription import PlanIn, PlanOut, PlanUpdate, SubscriptionOut
from app.schemas.trip import (
    ManifestPassenger,
    TripAdminOut,
    TripCancelRequest,
    TripCreate,
    TripManifest,
    TripUpdate,
)
from app.services import analytics as analytics_service
from app.services import audit as audit_service
from app.services import bookings as booking_service
from app.services import notifications as notification_service
from app.services import payments as payment_service
from app.services.trips import (
    generate_trips_from_templates,
    get_trip_or_404,
    lock_trip,
    release_expired_holds,
    search_trips,
    to_trip_out,
    trip_revenue_map,
)

router = APIRouter(prefix="/admin", tags=["Admin"])


# ══════════════════════════════════════════════════════════════
#  Routes & stops
# ══════════════════════════════════════════════════════════════


@router.get(
    "/routes",
    response_model=list[RouteAdminOut],
    summary="All routes, with whether each is actually sellable",
)
async def admin_list_routes(db: DbSession, admin: AdminUser) -> list[RouteAdminOut]:
    routes = list((await db.execute(select(Route).order_by(Route.name))).unique().scalars().all())
    if not routes:
        return []

    ids = [r.id for r in routes]

    template_rows = (
        await db.execute(
            select(
                TripTemplate.route_id,
                func.count(TripTemplate.id),
                func.coalesce(func.sum(case((TripTemplate.is_active, 1), else_=0)), 0),
            )
            .where(TripTemplate.route_id.in_(ids))
            .group_by(TripTemplate.route_id)
        )
    ).all()
    templates = {r[0]: (int(r[1]), int(r[2])) for r in template_rows}

    trip_rows = (
        await db.execute(
            select(Trip.route_id, func.count(Trip.id))
            .where(
                and_(
                    Trip.route_id.in_(ids),
                    Trip.departure_datetime >= now_utc(),
                    Trip.status != TripStatus.CANCELLED,
                )
            )
            .group_by(Trip.route_id)
        )
    ).all()
    upcoming = {r[0]: int(r[1]) for r in trip_rows}

    out: list[RouteAdminOut] = []
    for route in routes:
        total_templates, active_templates = templates.get(route.id, (0, 0))
        trips_ahead = upcoming.get(route.id, 0)

        if not route.is_active:
            readiness, hint = "withdrawn", "Not shown on the website."
        elif route.service_type == "charter":
            readiness, hint = "charter", "Charter routes are quoted, not sold by the seat."
        elif active_templates == 0:
            readiness, hint = (
                "no_timetable",
                "Add a timetable entry so departures can be generated.",
            )
        elif trips_ahead == 0:
            readiness, hint = (
                "no_departures",
                "Timetable is set — run Generate to publish departures.",
            )
        else:
            readiness, hint = "bookable", None

        row = RouteAdminOut.model_validate(route)
        row.template_count = total_templates
        row.active_template_count = active_templates
        row.upcoming_trip_count = trips_ahead
        row.is_bookable = readiness == "bookable"
        row.readiness = readiness
        row.readiness_hint = hint
        out.append(row)

    return out


@router.post("/routes", response_model=RouteOut, status_code=status.HTTP_201_CREATED)
async def create_route(
    payload: RouteIn, db: DbSession, admin: AdminUser, request: Request
) -> RouteOut:
    exists = (
        await db.execute(select(Route).where(Route.code == payload.code.upper()))
    ).unique().scalar_one_or_none()
    if exists:
        raise ConflictError("A route with that code already exists.")
    route = Route(**{**payload.model_dump(), "code": payload.code.upper()})
    db.add(route)
    await db.flush()
    await audit_service.record(
        db, actor=admin, action="route.create", entity_type="route",
        entity_id=route.id, entity_label=route.name, request=request,
        summary=f"Created the route {route.name} ({route.code}) at {naira(route.base_fare_kobo)} a seat.",
    )
    await db.commit()
    await db.refresh(route)
    return RouteOut.model_validate(route)


@router.patch("/routes/{route_id}", response_model=RouteOut)
async def update_route(
    route_id: uuid.UUID, payload: RouteUpdate, db: DbSession, admin: AdminUser, request: Request
) -> RouteOut:
    route = await db.get(Route, route_id)
    if route is None:
        raise NotFoundError("That route could not be found.")

    data = payload.model_dump(exclude_unset=True)
    before = {k: getattr(route, k) for k in data}
    for key, value in data.items():
        setattr(route, key, value)

    await audit_service.record(
        db, actor=admin, action="route.update", entity_type="route",
        entity_id=route.id, entity_label=route.name, request=request,
        changes=audit_service.diff(before, data),
        summary=f"Updated {route.name} ({', '.join(data) or 'no fields'}).",
    )
    await db.commit()
    await db.refresh(route)
    return RouteOut.model_validate(route)


@router.delete("/routes/{route_id}", response_model=Message)
async def deactivate_route(
    route_id: uuid.UUID, db: DbSession, admin: AdminUser, request: Request
) -> Message:
    route = await db.get(Route, route_id)
    if route is None:
        raise NotFoundError("That route could not be found.")
    # Soft-delete: historical trips and bookings must keep referring to it.
    route.is_active = False
    await audit_service.record(
        db, actor=admin, action="route.deactivate", entity_type="route",
        entity_id=route.id, entity_label=route.name, request=request,
        summary=f"Withdrew {route.name} from sale. It no longer appears on the website.",
    )
    await db.commit()
    return Message(message=f"{route.name} deactivated.")


@router.post("/routes/{route_id}/stops", response_model=RouteStopOut, status_code=status.HTTP_201_CREATED)
async def add_stop(
    route_id: uuid.UUID, payload: RouteStopIn, db: DbSession, admin: AdminUser, request: Request
) -> RouteStopOut:
    route = await db.get(Route, route_id)
    if route is None:
        raise NotFoundError("That route could not be found.")
    stop = RouteStop(route_id=route_id, **payload.model_dump())
    db.add(stop)
    await db.flush()
    await audit_service.record(
        db, actor=admin, action="stop.create", entity_type="route",
        entity_id=route.id, entity_label=route.name, request=request,
        summary=f"Added the stop \"{stop.name}\" to {route.name}.",
    )
    await db.commit()
    await db.refresh(stop)
    return RouteStopOut.model_validate(stop)


@router.patch("/stops/{stop_id}", response_model=RouteStopOut)
async def update_stop(
    stop_id: uuid.UUID, payload: RouteStopIn, db: DbSession, admin: AdminUser
) -> RouteStopOut:
    stop = await db.get(RouteStop, stop_id)
    if stop is None:
        raise NotFoundError("That stop could not be found.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(stop, key, value)
    await db.commit()
    await db.refresh(stop)
    return RouteStopOut.model_validate(stop)


@router.delete("/stops/{stop_id}", response_model=Message)
async def delete_stop(
    stop_id: uuid.UUID, db: DbSession, admin: AdminUser, request: Request
) -> Message:
    stop = await db.get(RouteStop, stop_id)
    if stop is None:
        raise NotFoundError("That stop could not be found.")
    name = stop.name
    await audit_service.record(
        db, actor=admin, action="stop.delete", entity_type="route",
        entity_id=stop.route_id, entity_label=name, request=request,
        summary=f"Removed the stop \"{name}\".",
    )
    await db.delete(stop)
    await db.commit()
    return Message(message="Stop removed.")


# ══════════════════════════════════════════════════════════════
#  Vehicles
# ══════════════════════════════════════════════════════════════


@router.get("/vehicles", response_model=list[VehicleOut])
async def list_vehicles(db: DbSession, admin: AdminUser) -> list[VehicleOut]:
    rows = (await db.execute(select(Vehicle).order_by(Vehicle.name))).scalars().all()
    return [VehicleOut.model_validate(v) for v in rows]


@router.post("/vehicles", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
async def create_vehicle(
    payload: VehicleIn, db: DbSession, admin: AdminUser, request: Request
) -> VehicleOut:
    vehicle = Vehicle(**payload.model_dump())
    db.add(vehicle)
    await db.flush()
    await audit_service.record(
        db, actor=admin, action="vehicle.create", entity_type="vehicle",
        entity_id=vehicle.id, entity_label=vehicle.name, request=request,
        summary=f"Added the vehicle {vehicle.name} ({vehicle.plate_no}), {vehicle.seat_capacity} seats.",
    )
    await db.commit()
    await db.refresh(vehicle)
    return VehicleOut.model_validate(vehicle)


@router.patch("/vehicles/{vehicle_id}", response_model=VehicleOut)
async def update_vehicle(
    vehicle_id: uuid.UUID, payload: VehicleUpdate, db: DbSession, admin: AdminUser, request: Request
) -> VehicleOut:
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise NotFoundError("That vehicle could not be found.")

    data = payload.model_dump(exclude_unset=True)
    before = {k: getattr(vehicle, k) for k in data}
    for key, value in data.items():
        setattr(vehicle, key, value)

    await audit_service.record(
        db, actor=admin, action="vehicle.update", entity_type="vehicle",
        entity_id=vehicle.id, entity_label=vehicle.name, request=request,
        changes=audit_service.diff(before, data),
        summary=(
            f"Set {vehicle.name} to {data['status']}."
            if "status" in data and len(data) == 1
            else f"Updated {vehicle.name} ({', '.join(data)})."
        ),
    )
    await db.commit()
    await db.refresh(vehicle)
    return VehicleOut.model_validate(vehicle)


@router.delete("/vehicles/{vehicle_id}", response_model=Message)
async def retire_vehicle(vehicle_id: uuid.UUID, db: DbSession, admin: AdminUser) -> Message:
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise NotFoundError("That vehicle could not be found.")
    vehicle.status = "retired"
    await db.commit()
    return Message(message=f"{vehicle.name} retired.")


# ══════════════════════════════════════════════════════════════
#  Drivers
# ══════════════════════════════════════════════════════════════


def _driver_out(driver: Driver) -> DriverOut:
    return DriverOut(
        id=driver.id,
        user_id=driver.user_id,
        full_name=driver.user.full_name,
        phone=driver.user.phone,
        email=driver.user.email,
        license_no=driver.license_no,
        photo_url=driver.photo_url,
        assigned_vehicle_id=driver.assigned_vehicle_id,
        assigned_vehicle_name=driver.assigned_vehicle.name if driver.assigned_vehicle else None,
        status=driver.status,
        is_active=driver.user.is_active,
    )


@router.get("/drivers", response_model=list[DriverOut])
async def list_drivers(db: DbSession, admin: AdminUser) -> list[DriverOut]:
    rows = list((await db.execute(select(Driver))).unique().scalars().all())
    return [_driver_out(d) for d in rows]


@router.post(
    "/drivers",
    response_model=DriverOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a driver, including their portal login",
)
async def create_driver(
    payload: DriverIn, db: DbSession, admin: AdminUser, request: Request
) -> DriverOut:
    clash = (
        await db.execute(select(User).where(User.phone == payload.phone))
    ).unique().scalar_one_or_none()
    if clash and clash.role != UserRole.PASSENGER:
        raise ConflictError("A staff account already uses that phone number.")

    if clash:
        user = clash
        user.full_name = payload.full_name
        user.role = UserRole.DRIVER
        user.password_hash = hash_password(payload.password)
        user.email = payload.email or user.email
    else:
        user = User(
            full_name=payload.full_name,
            phone=payload.phone,
            email=payload.email,
            password_hash=hash_password(payload.password),
            role=UserRole.DRIVER,
            phone_verified=True,
        )
        db.add(user)
    await db.flush()

    driver = Driver(
        user_id=user.id,
        license_no=payload.license_no,
        photo_url=payload.photo_url,
        assigned_vehicle_id=payload.assigned_vehicle_id,
        status=payload.status,
    )
    db.add(driver)
    await db.flush()
    await audit_service.record(
        db, actor=admin, action="driver.create", entity_type="driver",
        entity_id=driver.id, entity_label=payload.full_name, request=request,
        summary=f"Created a driver account for {payload.full_name} (licence {payload.license_no}).",
    )
    await db.commit()
    await db.refresh(driver)
    return _driver_out(driver)


@router.patch("/drivers/{driver_id}", response_model=DriverOut)
async def update_driver(
    driver_id: uuid.UUID, payload: DriverUpdate, db: DbSession, admin: AdminUser
) -> DriverOut:
    driver = (
        await db.execute(select(Driver).where(Driver.id == driver_id))
    ).unique().scalar_one_or_none()
    if driver is None:
        raise NotFoundError("That driver could not be found.")

    data = payload.model_dump(exclude_unset=True)
    for field in ("full_name", "email", "is_active"):
        if field in data and data[field] is not None:
            setattr(driver.user, field, data.pop(field))
        else:
            data.pop(field, None)
    if data.get("password"):
        driver.user.password_hash = hash_password(data.pop("password"))
    data.pop("password", None)

    for key, value in data.items():
        setattr(driver, key, value)

    await db.commit()
    await db.refresh(driver)
    return _driver_out(driver)


@router.delete("/drivers/{driver_id}", response_model=Message)
async def suspend_driver(
    driver_id: uuid.UUID, db: DbSession, admin: AdminUser, request: Request
) -> Message:
    driver = (
        await db.execute(select(Driver).where(Driver.id == driver_id))
    ).unique().scalar_one_or_none()
    if driver is None:
        raise NotFoundError("That driver could not be found.")
    driver.status = "suspended"
    driver.user.is_active = False
    await audit_service.record(
        db, actor=admin, action="driver.suspend", entity_type="driver",
        entity_id=driver.id, entity_label=driver.user.full_name, request=request,
        summary=f"Suspended {driver.user.full_name}. They can no longer sign in to the driver portal.",
    )
    await db.commit()
    return Message(message=f"{driver.user.full_name} suspended.")


# ══════════════════════════════════════════════════════════════
#  Timetable templates
# ══════════════════════════════════════════════════════════════


def _template_out(t: TripTemplate) -> TripTemplateOut:
    return TripTemplateOut(
        id=t.id,
        route_id=t.route_id,
        route_name=t.route.name if t.route else None,
        departure_time=t.departure_time,
        days_of_week=t.days_of_week,
        vehicle_id=t.vehicle_id,
        vehicle_name=t.vehicle.name if t.vehicle else None,
        driver_id=t.driver_id,
        driver_name=t.driver.user.full_name if t.driver and t.driver.user else None,
        fare_override_kobo=t.fare_override_kobo,
        is_active=t.is_active,
    )


@router.get("/templates", response_model=list[TripTemplateOut], summary="The timetable")
async def list_templates(
    db: DbSession, admin: AdminUser, route_id: uuid.UUID | None = None
) -> list[TripTemplateOut]:
    stmt = select(TripTemplate).order_by(TripTemplate.departure_time)
    if route_id:
        stmt = stmt.where(TripTemplate.route_id == route_id)
    rows = list((await db.execute(stmt)).unique().scalars().all())
    return [_template_out(t) for t in rows]


@router.post("/templates", response_model=TripTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    payload: TripTemplateIn, db: DbSession, admin: AdminUser, request: Request
) -> TripTemplateOut:
    if not payload.days_of_week or any(d < 1 or d > 7 for d in payload.days_of_week):
        raise ValidationError("Days of week must be ISO weekday numbers between 1 (Mon) and 7 (Sun).")
    if await db.get(Route, payload.route_id) is None:
        raise NotFoundError("That route could not be found.")

    template = TripTemplate(**payload.model_dump())
    db.add(template)
    await db.flush()
    route_row = await db.get(Route, payload.route_id)
    await audit_service.record(
        db, actor=admin, action="timetable.create", entity_type="template",
        entity_id=template.id, entity_label=route_row.name if route_row else None, request=request,
        summary=(
            f"Added a {payload.departure_time.strftime('%H:%M')} departure to "
            f"{route_row.name if route_row else 'a route'} on "
            f"{len(payload.days_of_week)} day(s) a week."
        ),
    )
    await db.commit()
    await db.refresh(template)
    return _template_out(template)


@router.patch("/templates/{template_id}", response_model=TripTemplateOut)
async def update_template(
    template_id: uuid.UUID,
    payload: TripTemplateUpdate,
    db: DbSession,
    admin: AdminUser,
    request: Request,
) -> TripTemplateOut:
    template = (
        await db.execute(select(TripTemplate).where(TripTemplate.id == template_id))
    ).unique().scalar_one_or_none()
    if template is None:
        raise NotFoundError("That timetable entry could not be found.")
    data = payload.model_dump(exclude_unset=True)
    before = {k: getattr(template, k) for k in data}
    for key, value in data.items():
        setattr(template, key, value)

    await audit_service.record(
        db, actor=admin, action="timetable.update", entity_type="template",
        entity_id=template.id,
        entity_label=template.route.name if template.route else None, request=request,
        changes=audit_service.diff(before, data),
        summary=(
            f"{'Activated' if data['is_active'] else 'Paused'} the "
            f"{template.departure_time.strftime('%H:%M')} departure on "
            f"{template.route.name if template.route else 'a route'}."
            if "is_active" in data and len(data) == 1
            else f"Updated the {template.departure_time.strftime('%H:%M')} timetable entry."
        ),
    )
    await db.commit()
    await db.refresh(template)
    return _template_out(template)


@router.delete("/templates/{template_id}", response_model=Message)
async def deactivate_template(template_id: uuid.UUID, db: DbSession, admin: AdminUser) -> Message:
    template = await db.get(TripTemplate, template_id)
    if template is None:
        raise NotFoundError("That timetable entry could not be found.")
    template.is_active = False
    await db.commit()
    return Message(message="Timetable entry deactivated. Existing trips are unaffected.")


@router.post(
    "/templates/generate",
    response_model=Message,
    summary="Materialise trips from the timetable now",
)
async def generate_trips(
    db: DbSession, admin: AdminUser, request: Request, days_ahead: int = Query(14, ge=1, le=90)
) -> Message:
    created = await generate_trips_from_templates(db, days_ahead=days_ahead)
    await audit_service.record(
        db, actor=admin, action="timetable.generate", entity_type="template",
        request=request,
        summary=f"Generated {created} departure(s) from the timetable for the next {days_ahead} days.",
    )
    await db.commit()
    return Message(message=f"{created} trip(s) generated for the next {days_ahead} days.")


# ══════════════════════════════════════════════════════════════
#  Trips
# ══════════════════════════════════════════════════════════════


async def _trip_admin_out(db, trips: list[Trip]) -> list[TripAdminOut]:
    stats = await trip_revenue_map(db, [t.id for t in trips])
    out: list[TripAdminOut] = []
    for trip in trips:
        base = to_trip_out(trip).model_dump()
        count, revenue = stats.get(trip.id, (0, 0))
        out.append(
            TripAdminOut(
                **base,
                template_id=trip.template_id,
                cancellation_reason=trip.cancellation_reason,
                confirmed_bookings=count,
                revenue_kobo=revenue,
            )
        )
    return out


@router.get("/trips", response_model=list[TripAdminOut], summary="Trips with revenue and occupancy")
async def admin_list_trips(
    db: DbSession,
    admin: AdminUser,
    route_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    trip_status: str | None = Query(None, alias="status"),
) -> list[TripAdminOut]:
    await release_expired_holds(db)
    await db.commit()

    date_from = date_from or today_lagos()
    date_to = date_to or (date_from + timedelta(days=14))
    trips = await search_trips(
        db,
        route_id=route_id,
        date_from=date_from,
        date_to=date_to,
        include_past=True,
        include_cancelled=True,
        limit=500,
    )
    if trip_status:
        trips = [t for t in trips if t.status == trip_status]
    return await _trip_admin_out(db, trips)


@router.post("/trips", response_model=TripAdminOut, status_code=status.HTTP_201_CREATED)
async def create_trip(payload: TripCreate, db: DbSession, admin: AdminUser) -> TripAdminOut:
    route = await db.get(Route, payload.route_id)
    if route is None:
        raise NotFoundError("That route could not be found.")

    capacity = payload.seats_total
    if capacity is None and payload.vehicle_id:
        vehicle = await db.get(Vehicle, payload.vehicle_id)
        capacity = vehicle.seat_capacity if vehicle else 14

    departure = payload.departure_datetime
    trip = Trip(
        route_id=route.id,
        service_date=departure.astimezone(LAGOS).date(),
        departure_datetime=departure,
        arrival_estimate=departure + timedelta(minutes=route.duration_mins),
        vehicle_id=payload.vehicle_id,
        driver_id=payload.driver_id,
        status=TripStatus.SCHEDULED,
        seats_total=capacity or 14,
        seats_booked=0,
        fare_kobo=payload.fare_kobo or route.base_fare_kobo,
    )
    db.add(trip)
    await db.commit()
    await db.refresh(trip)
    return (await _trip_admin_out(db, [trip]))[0]


@router.patch(
    "/trips/{trip_id}",
    response_model=TripAdminOut,
    summary="Edit a trip, optionally broadcasting the change to every passenger",
)
async def update_trip(
    trip_id: uuid.UUID, payload: TripUpdate, db: DbSession, admin: AdminUser, request: Request
) -> TripAdminOut:
    trip = await lock_trip(db, trip_id)
    data = payload.model_dump(exclude_unset=True)
    notify = data.pop("notify_passengers", False)
    message = data.pop("message", None)

    if "seats_total" in data and data["seats_total"] is not None:
        if data["seats_total"] < trip.seats_booked:
            raise ConflictError(
                f"{trip.seats_booked} seat(s) are already sold — capacity cannot go below that."
            )

    old_departure = trip.departure_datetime
    for key, value in data.items():
        if value is not None:
            setattr(trip, key, value)

    if "departure_datetime" in data and data["departure_datetime"]:
        trip.service_date = trip.departure_datetime.astimezone(LAGOS).date()
        route = await db.get(Route, trip.route_id)
        trip.arrival_estimate = trip.departure_datetime + timedelta(minutes=route.duration_mins)

    await db.flush()

    if notify:
        route = await db.get(Route, trip.route_id)
        change = message or (
            f"Your departure has moved from {fmt_datetime(old_departure)} to "
            f"{fmt_datetime(trip.departure_datetime)}."
        )
        for booking in await booking_service.bookings_for_trip(db, trip.id):
            await notification_service.send_schedule_change(db, booking, trip, route, change)

    await audit_service.record(
        db, actor=admin, action="trip.update", entity_type="trip",
        entity_id=trip.id, entity_label=trip.route.name if trip.route else None, request=request,
        summary=(
            f"Updated the {fmt_datetime(trip.departure_datetime)} departure"
            + (" and notified every passenger." if notify else ".")
        ),
    )
    await db.commit()
    await db.refresh(trip)
    return (await _trip_admin_out(db, [trip]))[0]


@router.post(
    "/trips/{trip_id}/cancel",
    response_model=Message,
    summary="Cancel a departure and notify every passenger",
)
async def cancel_trip(
    trip_id: uuid.UUID,
    payload: TripCancelRequest,
    db: DbSession,
    admin: AdminUser,
    request: Request,
) -> Message:
    trip = await lock_trip(db, trip_id)
    if trip.status == TripStatus.CANCELLED:
        raise ConflictError("That departure is already cancelled.")

    route = await db.get(Route, trip.route_id)
    affected = await booking_service.bookings_for_trip(db, trip.id)

    trip.status = TripStatus.CANCELLED
    trip.cancellation_reason = payload.reason

    for booking in affected:
        await booking_service.cancel_booking(
            db, booking, reason=f"Trip cancelled: {payload.reason}", by_admin=True
        )
        if payload.notify_passengers:
            await notification_service.send_schedule_change(
                db,
                booking,
                trip,
                route,
                f"This departure has been cancelled. Reason: {payload.reason}",
                cancelled=True,
            )

    notified = len(affected) if payload.notify_passengers else 0
    await audit_service.record(
        db, actor=admin, action="trip.cancel", entity_type="trip",
        entity_id=trip.id, entity_label=route.name if route else None, request=request,
        summary=(
            f"Cancelled the {fmt_datetime(trip.departure_datetime)} "
            f"{route.name if route else ''} departure — {payload.reason}. "
            f"{len(affected)} booking(s) released, {notified} passenger(s) notified."
        ),
    )
    await db.commit()
    return Message(
        message=f"Departure cancelled. {len(affected)} booking(s) released, {notified} passenger(s) notified."
    )


@router.get("/trips/{trip_id}/manifest", response_model=TripManifest, summary="Passenger manifest")
async def trip_manifest(trip_id: uuid.UUID, db: DbSession, admin: AdminUser) -> TripManifest:
    return await build_manifest(db, trip_id)


async def build_manifest(db, trip_id: uuid.UUID) -> TripManifest:
    trip = await get_trip_or_404(db, trip_id)
    route = await db.get(Route, trip.route_id)
    rows = await booking_service.bookings_for_trip(db, trip_id)

    ticket_map = {}
    if rows:
        tickets = (
            await db.execute(select(Ticket).where(Ticket.booking_id.in_([b.id for b in rows])))
        ).scalars().all()
        ticket_map = {t.booking_id: t for t in tickets}

    stop_ids = [b.pickup_stop_id for b in rows if b.pickup_stop_id]
    stop_names: dict[uuid.UUID, str] = {}
    if stop_ids:
        stops = (
            await db.execute(select(RouteStop).where(RouteStop.id.in_(stop_ids)))
        ).scalars().all()
        stop_names = {s.id: s.name for s in stops}

    passengers = [
        ManifestPassenger(
            booking_ref=b.booking_ref,
            passenger_name=b.passenger_name,
            passenger_phone=b.passenger_phone,
            passenger_email=b.passenger_email,
            seats=b.seats,
            seat_numbers=b.seat_numbers or [],
            status=b.status,
            source=b.source,
            amount_kobo=b.amount_kobo,
            checked_in_at=ticket_map.get(b.id).checked_in_at if ticket_map.get(b.id) else None,
            pickup_stop=stop_names.get(b.pickup_stop_id) if b.pickup_stop_id else None,
        )
        for b in rows
    ]

    return TripManifest(
        trip=to_trip_out(trip, route),
        passengers=passengers,
        total_passengers=sum(p.seats for p in passengers),
        checked_in_count=sum(1 for p in passengers if p.checked_in_at),
        revenue_kobo=sum(p.amount_kobo for p in passengers),
    )


# ══════════════════════════════════════════════════════════════
#  Bookings
# ══════════════════════════════════════════════════════════════


@router.get("/bookings", response_model=Page[BookingOut], summary="Searchable booking table")
async def admin_list_bookings(
    db: DbSession,
    admin: AdminUser,
    q: str | None = Query(None, description="Matches reference, name, phone or email"),
    booking_status: str | None = Query(None, alias="status"),
    source: str | None = None,
    trip_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> Page[BookingOut]:
    stmt = select(Booking)
    filters = []

    if q:
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Booking.booking_ref.ilike(like),
                Booking.passenger_name.ilike(like),
                Booking.passenger_phone.ilike(like),
                Booking.passenger_email.ilike(like),
            )
        )
    if booking_status:
        filters.append(Booking.status == booking_status)
    if source:
        filters.append(Booking.source == source)
    if trip_id:
        filters.append(Booking.trip_id == trip_id)
    if date_from or date_to:
        stmt = stmt.join(Trip, Trip.id == Booking.trip_id)
        if date_from:
            filters.append(Trip.service_date >= date_from)
        if date_to:
            filters.append(Trip.service_date <= date_to)

    if filters:
        stmt = stmt.where(and_(*filters))

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()

    rows = list(
        (
            await db.execute(
                stmt.order_by(desc(Booking.created_at)).offset((page - 1) * page_size).limit(page_size)
            )
        ).unique().scalars().all()
    )

    items = []
    for booking in rows:
        out = BookingOut.model_validate(booking)
        trip = await db.get(Trip, booking.trip_id)
        if trip:
            route = await db.get(Route, trip.route_id)
            out.trip = to_trip_out(trip, route)
        items.append(out)

    return Page[BookingOut](items=items, total=total, page=page, page_size=page_size)


@router.post(
    "/bookings",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a booking on a passenger's behalf (phone/walk-in sales)",
)
async def admin_create_booking(
    payload: AdminBookingCreate, db: DbSession, admin: AdminUser, request: Request
) -> BookingOut:
    linked = await booking_service.find_or_create_passenger(
        db,
        full_name=payload.passenger_name,
        phone=payload.passenger_phone,
        email=payload.passenger_email,
    )
    booking = await booking_service.create_booking(
        db,
        trip_id=payload.trip_id,
        passenger_name=payload.passenger_name,
        passenger_phone=payload.passenger_phone,
        passenger_email=payload.passenger_email,
        seats=payload.seats,
        seats_male=payload.seats_male,
        seats_female=payload.seats_female,
        source=BookingSource.ADMIN,
        user_id=linked.id,
        pickup_stop_id=payload.pickup_stop_id,
        notes=payload.notes,
        amount_kobo_override=payload.amount_kobo_override,
    )

    if payload.mark_confirmed:
        await booking_service.confirm_booking(db, booking)
    else:
        await payment_service.initialize_booking_payment(db, booking, payload.passenger_email)

    await audit_service.record(
        db, actor=admin, action="booking.create", entity_type="booking",
        entity_id=booking.id, entity_label=booking.booking_ref, request=request,
        summary=(
            f"Created booking {booking.booking_ref} at the desk for {booking.passenger_name} "
            f"({booking.seats} seat(s), {naira(booking.amount_kobo)})"
            + (", marked as already paid." if payload.mark_confirmed else ", awaiting payment.")
        ),
    )
    await db.commit()
    await db.refresh(booking)

    out = BookingOut.model_validate(booking)
    trip = await db.get(Trip, booking.trip_id)
    out.trip = to_trip_out(trip, await db.get(Route, trip.route_id))
    return out


@router.post("/bookings/{booking_ref}/confirm", response_model=Message, summary="Force-confirm a booking")
async def admin_confirm_booking(
    booking_ref: str, db: DbSession, admin: AdminUser, request: Request
) -> Message:
    booking = await booking_service.get_booking_by_ref(db, booking_ref)
    await booking_service.confirm_booking(db, booking)
    await audit_service.record(
        db, actor=admin, action="booking.confirm", entity_type="booking",
        entity_id=booking.id, entity_label=booking.booking_ref, request=request,
        summary=(
            f"Force-confirmed {booking.booking_ref} without an online payment "
            f"({naira(booking.amount_kobo)}) and issued the ticket."
        ),
    )
    await db.commit()
    return Message(message=f"{booking.booking_ref} confirmed and ticket issued.")


# ══════════════════════════════════════════════════════════════
#  Subscription plans & subscribers
# ══════════════════════════════════════════════════════════════


@router.get("/plans", response_model=list[PlanOut])
async def admin_list_plans(db: DbSession, admin: AdminUser) -> list[PlanOut]:
    rows = (
        await db.execute(select(SubscriptionPlan).order_by(SubscriptionPlan.sort_order))
    ).scalars().all()
    return [PlanOut.model_validate(p) for p in rows]


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(payload: PlanIn, db: DbSession, admin: AdminUser) -> PlanOut:
    plan = SubscriptionPlan(**{**payload.model_dump(), "code": payload.code.upper()})
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return PlanOut.model_validate(plan)


@router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: uuid.UUID, payload: PlanUpdate, db: DbSession, admin: AdminUser
) -> PlanOut:
    plan = await db.get(SubscriptionPlan, plan_id)
    if plan is None:
        raise NotFoundError("That plan could not be found.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, key, value)
    await db.commit()
    await db.refresh(plan)
    return PlanOut.model_validate(plan)


@router.get("/subscriptions", response_model=list[SubscriptionOut], summary="All subscribers")
async def list_subscriptions(
    db: DbSession, admin: AdminUser, subscription_status: str | None = Query(None, alias="status")
) -> list[SubscriptionOut]:
    from app.api.v1.subscriptions import _to_out

    stmt = select(Subscription).order_by(desc(Subscription.created_at))
    if subscription_status:
        stmt = stmt.where(Subscription.status == subscription_status)
    rows = list((await db.execute(stmt)).unique().scalars().all())
    return [_to_out(s) for s in rows]


# ══════════════════════════════════════════════════════════════
#  Users & settings
# ══════════════════════════════════════════════════════════════


@router.get("/users", response_model=Page[UserOut], summary="All users")
async def list_users(
    db: DbSession,
    admin: AdminUser,
    q: str | None = None,
    role: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> Page[UserOut]:
    stmt = select(User)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(User.full_name.ilike(like), User.phone.ilike(like), User.email.ilike(like))
        )
    if role:
        stmt = stmt.where(User.role == role)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = list(
        (
            await db.execute(
                stmt.order_by(desc(User.created_at)).offset((page - 1) * page_size).limit(page_size)
            )
        ).unique().scalars().all()
    )
    return Page[UserOut](
        items=[UserOut.model_validate(u) for u in rows], total=total, page=page, page_size=page_size
    )


@router.post(
    "/users/staff",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an operations or super-admin account",
)
async def create_staff(
    payload: DriverIn,
    db: DbSession,
    admin: SuperAdminUser,
    request: Request,
    role: str = Query(UserRole.OPERATIONS),
) -> UserOut:
    if role not in {UserRole.OPERATIONS, UserRole.SUPER_ADMIN}:
        raise ValidationError("Role must be `operations` or `super_admin`.")
    clash = (
        await db.execute(select(User).where(User.phone == payload.phone))
    ).unique().scalar_one_or_none()
    if clash:
        raise ConflictError("An account already uses that phone number.")

    user = User(
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=role,
        phone_verified=True,
    )
    db.add(user)
    await db.flush()
    await audit_service.record(
        db, actor=admin, action="staff.create", entity_type="user",
        entity_id=user.id, entity_label=user.full_name, request=request,
        summary=f"Created a {role.replace('_', ' ')} account for {user.full_name}.",
    )
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}/status", response_model=UserOut, summary="Activate or deactivate a user")
async def set_user_status(
    user_id: uuid.UUID,
    db: DbSession,
    admin: SuperAdminUser,
    request: Request,
    is_active: bool = Query(...),
) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("That user could not be found.")
    if user.id == admin.id:
        raise ValidationError("You can't deactivate your own account.")
    user.is_active = is_active
    await audit_service.record(
        db, actor=admin, action="staff.status", entity_type="user",
        entity_id=user.id, entity_label=user.full_name, request=request,
        summary=f"{'Reactivated' if is_active else 'Deactivated'} {user.full_name}'s account.",
    )
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.get("/notification-templates", response_model=list[dict], summary="Editable message templates")
async def list_notification_templates(db: DbSession, admin: AdminUser) -> list[dict]:
    rows = (
        await db.execute(select(NotificationTemplate).order_by(NotificationTemplate.key))
    ).scalars().all()
    return [
        {
            "id": str(t.id),
            "key": t.key,
            "channel": t.channel,
            "subject": t.subject,
            "body": t.body,
            "description": t.description,
        }
        for t in rows
    ]


@router.patch("/notification-templates/{template_id}", response_model=Message)
async def update_notification_template(
    template_id: uuid.UUID, body: dict, db: DbSession, admin: AdminUser
) -> Message:
    template = await db.get(NotificationTemplate, template_id)
    if template is None:
        raise NotFoundError("That template could not be found.")
    for key in ("subject", "body", "description"):
        if key in body:
            setattr(template, key, body[key])
    await db.commit()
    return Message(message="Template updated.")


@router.get("/audit-logs", response_model=Page[dict], summary="Who changed what")
async def audit_logs(
    db: DbSession,
    admin: AdminUser,
    q: str | None = Query(None, description="Matches the summary, actor or entity label"),
    action: str | None = Query(None, description="Exact action, e.g. `trip.cancel`"),
    entity_type: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> Page[dict]:
    stmt = select(AuditLog)
    filters = []

    if q:
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                AuditLog.summary.ilike(like),
                AuditLog.actor_name.ilike(like),
                AuditLog.entity_label.ilike(like),
                AuditLog.action.ilike(like),
            )
        )
    if action:
        filters.append(AuditLog.action == action)
    if entity_type:
        filters.append(AuditLog.entity_type == entity_type)
    if actor_user_id:
        filters.append(AuditLog.actor_user_id == actor_user_id)
    if date_from:
        filters.append(
            AuditLog.created_at >= datetime.combine(date_from, datetime.min.time()).replace(tzinfo=LAGOS)
        )
    if date_to:
        filters.append(
            AuditLog.created_at
            < datetime.combine(date_to + timedelta(days=1), datetime.min.time()).replace(tzinfo=LAGOS)
        )
    if filters:
        stmt = stmt.where(and_(*filters))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(desc(AuditLog.created_at)).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    return Page[dict](
        items=[
            {
                "id": str(r.id),
                "actor_name": r.actor_name,
                "actor_role": r.actor_role,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "entity_label": r.entity_label,
                "summary": r.summary,
                "changes": r.changes,
                "ip": r.ip,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/audit-logs/actions", response_model=list[dict], summary="Actions seen so far")
async def audit_actions(db: DbSession, admin: AdminUser) -> list[dict]:
    """Populates the filter dropdown from real data rather than a hard-coded list."""
    rows = (
        await db.execute(
            select(AuditLog.action, func.count(AuditLog.id))
            .group_by(AuditLog.action)
            .order_by(AuditLog.action)
        )
    ).all()
    return [{"action": r[0], "count": int(r[1])} for r in rows]


@router.get("/notifications", response_model=list[dict], summary="Notification delivery log")
async def notification_log(
    db: DbSession, admin: AdminUser, limit: int = Query(100, ge=1, le=500)
) -> list[dict]:
    rows = (
        await db.execute(select(Notification).order_by(desc(Notification.created_at)).limit(limit))
    ).scalars().all()
    return [
        {
            "id": str(n.id),
            "channel": n.channel,
            "type": n.type,
            "recipient": n.recipient,
            "status": n.status,
            "provider": n.provider,
            "error": n.error,
            "created_at": n.created_at.isoformat(),
            "sent_at": n.sent_at.isoformat() if n.sent_at else None,
        }
        for n in rows
    ]


# ══════════════════════════════════════════════════════════════
#  Analytics
# ══════════════════════════════════════════════════════════════


@router.get("/analytics/overview", summary="Dashboard: today's trips, revenue, alerts")
async def analytics_overview(db: DbSession, admin: AdminUser):
    return await analytics_service.overview(db)


@router.get("/analytics/revenue", summary="Revenue trend series")
async def analytics_revenue(
    db: DbSession,
    admin: AdminUser,
    start: date | None = None,
    end: date | None = None,
    granularity: str = Query("day", pattern="^(day|week|month)$"),
):
    end = end or today_lagos()
    start = start or (end - timedelta(days=29))
    return await analytics_service.revenue_series(db, start=start, end=end, granularity=granularity)


@router.get("/analytics/routes", summary="Route performance")
async def analytics_routes(
    db: DbSession, admin: AdminUser, start: date | None = None, end: date | None = None
):
    end = end or today_lagos()
    start = start or (end - timedelta(days=29))
    return await analytics_service.route_performance(db, start=start, end=end)


@router.get("/analytics/occupancy", summary="Seat occupancy by departure time slot")
async def analytics_occupancy(
    db: DbSession, admin: AdminUser, start: date | None = None, end: date | None = None
):
    end = end or today_lagos()
    start = start or (end - timedelta(days=29))
    return await analytics_service.occupancy_by_time_slot(db, start=start, end=end)


@router.get("/analytics/subscriptions", summary="Subscription sales by plan")
async def analytics_subscriptions(
    db: DbSession, admin: AdminUser, start: date | None = None, end: date | None = None
):
    end = end or today_lagos()
    start = start or (end - timedelta(days=364))
    return await analytics_service.subscription_sales(db, start=start, end=end)


@router.get("/analytics/channels", summary="Bookings by channel (web / WhatsApp / subscription)")
async def analytics_channels(
    db: DbSession, admin: AdminUser, start: date | None = None, end: date | None = None
):
    from datetime import datetime

    end = end or today_lagos()
    start = start or (end - timedelta(days=29))
    start_dt = datetime.combine(start, datetime.min.time()).replace(tzinfo=LAGOS)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time()).replace(tzinfo=LAGOS)
    return await analytics_service.channel_breakdown(db, start_dt, end_dt)


@router.get("/analytics/demographics", summary="Passenger sex split (state reporting)")
async def analytics_demographics(
    db: DbSession, admin: AdminUser, start: date | None = None, end: date | None = None
):
    end = end or today_lagos()
    start = start or (end - timedelta(days=29))
    return {
        "summary": await analytics_service.demographics(db, start=start, end=end),
        "by_route": await analytics_service.demographics_by_route(db, start=start, end=end),
    }


@router.get("/analytics/export.csv", summary="Export bookings as CSV", response_class=Response)
async def export_bookings_csv(
    db: DbSession, admin: AdminUser, start: date | None = None, end: date | None = None
) -> Response:
    end = end or today_lagos()
    start = start or (end - timedelta(days=29))
    rows = await analytics_service.bookings_export_rows(db, start=start, end=end)

    buf = io.StringIO()
    fields = [
        "booking_ref", "created_at", "passenger_name", "passenger_phone", "passenger_email",
        "route", "service_date", "departure", "seats", "seats_male", "seats_female",
        "seat_numbers", "amount_naira", "source", "status",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)

    filename = f"ecojindu-bookings-{start:%Y%m%d}-{end:%Y%m%d}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
