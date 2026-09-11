"""Charter lifecycle: request → quote → pay → assign → travel.

A charter is priced per vehicle rather than per seat, and runs on a bespoke
departure rather than a timetable slot — so it can't reuse `trips` up front.
Once it is paid for and a vehicle is assigned, a `Trip` *is* created, and from
that point the manifest, QR ticket and driver portal work exactly as they do
for a scheduled departure.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import log_event
from app.core.timeutil import LAGOS, combine_lagos, fmt_date, naira, now_utc, today_lagos
from app.models.charter import CharterRequest
from app.models.enums import CharterStatus, ServiceType, TripStatus
from app.models.fleet import Vehicle
from app.models.route import Route
from app.models.trip import Trip
from app.services.email import render_template, send_email
from app.services.sms import send_sms

logger = logging.getLogger("ecojindu.charter")

#: Same read-aloud-safe alphabet as booking refs — no 0/O/1/I.
_REF_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

#: How far ahead a charter must be requested, so operations can actually resource it.
MIN_LEAD_TIME_HOURS = 12

#: Where a charter departs from when the caller doesn't pick a published route.
DEFAULT_CHARTER_TIME = time(9, 0)


async def _unique_reference(db: AsyncSession) -> str:
    for _ in range(12):
        ref = "EJC-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(5))
        exists = (
            await db.execute(select(CharterRequest.id).where(CharterRequest.reference == ref))
        ).scalar_one_or_none()
        if exists is None:
            return ref
    raise ConflictError("Could not allocate a charter reference. Please try again.")


async def indicative_amount(db: AsyncSession, route_id: uuid.UUID | None, passengers: int) -> int | None:
    """A non-binding estimate so the form isn't a black box.

    Uses the route's configured charter rate where one exists; otherwise falls
    back to seat fare × vehicle capacity, which is what a whole-vehicle hire
    costs at list price. Returns None when there's nothing to base it on.
    """
    if route_id is None:
        return None
    route = await db.get(Route, route_id)
    if route is None:
        return None
    if route.charter_fare_kobo:
        return route.charter_fare_kobo

    capacity = (
        await db.execute(select(Vehicle.seat_capacity).where(Vehicle.status == "active").limit(1))
    ).scalar_one_or_none()
    if not capacity:
        return None
    return route.base_fare_kobo * max(capacity, passengers)


async def create_request(
    db: AsyncSession,
    *,
    contact_name: str,
    contact_phone: str,
    contact_email: str | None,
    organisation: str | None,
    route_id: uuid.UUID | None,
    origin_text: str,
    destination_text: str,
    service_date,
    preferred_time: time | None,
    passengers: int,
    return_trip: bool,
    notes: str | None,
) -> CharterRequest:
    if service_date < today_lagos():
        raise ValidationError("That date has already passed.")

    departure = combine_lagos(service_date, preferred_time or DEFAULT_CHARTER_TIME)
    if departure < now_utc() + timedelta(hours=MIN_LEAD_TIME_HOURS):
        raise ValidationError(
            f"Charters need at least {MIN_LEAD_TIME_HOURS} hours' notice so we can "
            "assign a vehicle and driver. Please pick a later date or time."
        )

    charter = CharterRequest(
        reference=await _unique_reference(db),
        status=CharterStatus.REQUESTED,
        contact_name=contact_name.strip(),
        contact_phone=contact_phone,
        contact_email=contact_email,
        organisation=organisation,
        route_id=route_id,
        origin_text=origin_text.strip(),
        destination_text=destination_text.strip(),
        service_date=service_date,
        preferred_time=preferred_time,
        passengers=passengers,
        return_trip=return_trip,
        notes=notes,
    )
    db.add(charter)
    await db.flush()

    log_event(
        logger,
        logging.INFO,
        "charter requested",
        ref=charter.reference,
        passengers=passengers,
        date=str(service_date),
    )
    await _notify_requested(db, charter)
    return charter


async def get_by_reference(db: AsyncSession, reference: str) -> CharterRequest:
    ref = reference.strip().upper()
    if not ref.startswith("EJC-"):
        ref = f"EJC-{ref.removeprefix('EJC')}"
    charter = (
        await db.execute(select(CharterRequest).where(CharterRequest.reference == ref))
    ).unique().scalar_one_or_none()
    if charter is None:
        raise NotFoundError("We couldn't find a charter with that reference.")
    return charter


async def lookup(db: AsyncSession, reference: str, phone: str) -> CharterRequest:
    charter = await get_by_reference(db, reference)
    if charter.contact_phone != phone:
        # Deliberately the same message as not-found: a reference alone must not
        # confirm a charter exists against someone else's number.
        raise NotFoundError("We couldn't find a charter with that reference and phone number.")
    return charter


async def quote(
    db: AsyncSession,
    charter: CharterRequest,
    *,
    amount_kobo: int,
    quote_notes: str | None,
    vehicle_id: uuid.UUID | None,
    quoted_by: uuid.UUID | None,
    notify: bool = True,
) -> CharterRequest:
    if charter.status in {CharterStatus.CONFIRMED, CharterStatus.ASSIGNED, CharterStatus.COMPLETED}:
        raise ConflictError("This charter has already been paid for and can't be re-quoted.")
    if charter.status == CharterStatus.CANCELLED:
        raise ConflictError("This charter was cancelled.")

    charter.quoted_amount_kobo = amount_kobo
    charter.quote_notes = quote_notes
    charter.quoted_by = quoted_by
    charter.quoted_at = now_utc()
    charter.status = CharterStatus.QUOTED
    if vehicle_id:
        charter.vehicle_id = vehicle_id
    await db.flush()

    log_event(logger, logging.INFO, "charter quoted", ref=charter.reference, amount=amount_kobo)
    if notify:
        await _notify_quoted(db, charter)
    return charter


async def mark_confirmed(db: AsyncSession, charter: CharterRequest) -> CharterRequest:
    """Called when the charter's payment settles. Idempotent."""
    if charter.status in {CharterStatus.CONFIRMED, CharterStatus.ASSIGNED, CharterStatus.COMPLETED}:
        return charter

    charter.status = CharterStatus.CONFIRMED
    charter.confirmed_at = now_utc()
    await db.flush()

    log_event(logger, logging.INFO, "charter confirmed", ref=charter.reference)
    await _notify_confirmed(db, charter)
    return charter


async def assign(
    db: AsyncSession,
    charter: CharterRequest,
    *,
    vehicle_id: uuid.UUID,
    driver_id: uuid.UUID | None,
    create_trip: bool = True,
) -> CharterRequest:
    if charter.status not in {CharterStatus.CONFIRMED, CharterStatus.ASSIGNED}:
        raise ConflictError("Assign a vehicle only once the charter has been paid for.")

    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise NotFoundError("That vehicle could not be found.")
    if vehicle.seat_capacity < charter.passengers:
        raise ConflictError(
            f"{vehicle.name} seats {vehicle.seat_capacity} but the charter is for "
            f"{charter.passengers} passengers."
        )

    charter.vehicle_id = vehicle_id
    charter.driver_id = driver_id
    charter.status = CharterStatus.ASSIGNED

    if create_trip and charter.trip_id is None:
        charter.trip_id = (await _materialise_trip(db, charter, vehicle)).id

    await db.flush()
    log_event(
        logger, logging.INFO, "charter assigned", ref=charter.reference, vehicle=vehicle.name
    )
    return charter


async def _materialise_trip(db: AsyncSession, charter: CharterRequest, vehicle: Vehicle) -> Trip:
    """Turn a paid charter into a real Trip.

    Seats are marked fully taken because the whole vehicle is hired — this stops
    it ever appearing in public seat availability while still giving operations
    a manifest and the driver a run on their portal.
    """
    departure = combine_lagos(charter.service_date, charter.preferred_time or DEFAULT_CHARTER_TIME)
    route = await db.get(Route, charter.route_id) if charter.route_id else None
    duration = route.duration_mins if route else 60

    if route is None:
        # A bespoke charter still needs a route row to hang the trip off.
        route = await _ad_hoc_route(db, charter)

    trip = Trip(
        route_id=route.id,
        service_date=charter.service_date,
        departure_datetime=departure,
        arrival_estimate=departure + timedelta(minutes=duration),
        vehicle_id=vehicle.id,
        driver_id=charter.driver_id,
        status=TripStatus.SCHEDULED,
        seats_total=vehicle.seat_capacity,
        seats_booked=vehicle.seat_capacity,
        fare_kobo=charter.quoted_amount_kobo or 0,
    )
    db.add(trip)
    await db.flush()
    return trip


async def _ad_hoc_route(db: AsyncSession, charter: CharterRequest) -> Route:
    """A hidden charter-only route for bespoke origin/destination pairs."""
    code = f"CHT-{charter.reference.removeprefix('EJC-')}"
    existing = (
        await db.execute(select(Route).where(Route.code == code))
    ).unique().scalar_one_or_none()
    if existing:
        return existing

    route = Route(
        name=f"{charter.origin_text} → {charter.destination_text}",
        code=code,
        origin_terminal=charter.origin_text,
        destination=charter.destination_text,
        distance_km=0,
        duration_mins=60,
        base_fare_kobo=charter.quoted_amount_kobo or 0,
        service_type=ServiceType.CHARTER,
        # Never listed publicly — it exists only to anchor this charter's trip.
        is_active=False,
        description=f"Ad-hoc charter route for {charter.reference}",
    )
    db.add(route)
    await db.flush()
    return route


async def cancel(
    db: AsyncSession,
    charter: CharterRequest,
    *,
    reason: str,
    declined: bool = False,
    notify: bool = True,
) -> CharterRequest:
    if charter.status in {CharterStatus.CANCELLED, CharterStatus.DECLINED}:
        return charter
    if charter.status == CharterStatus.COMPLETED:
        raise ConflictError("This charter has already been travelled.")

    charter.status = CharterStatus.DECLINED if declined else CharterStatus.CANCELLED
    charter.cancelled_at = now_utc()
    charter.cancellation_reason = reason

    if charter.trip_id:
        trip = await db.get(Trip, charter.trip_id)
        if trip:
            trip.status = TripStatus.CANCELLED
            trip.cancellation_reason = f"Charter {charter.reference} cancelled: {reason}"

    await db.flush()
    log_event(logger, logging.INFO, "charter cancelled", ref=charter.reference, declined=declined)
    if notify:
        await _notify_cancelled(db, charter, reason)
    return charter


# ── Notifications ────────────────────────────────────────────


def _summary_rows(charter: CharterRequest) -> list[tuple[str, str]]:
    rows = [
        ("Reference", charter.reference),
        ("From", charter.origin_text),
        ("To", charter.destination_text),
        ("Date", fmt_date(charter.service_date)),
        ("Passengers", str(charter.passengers)),
    ]
    if charter.preferred_time:
        rows.insert(4, ("Preferred time", charter.preferred_time.strftime("%I:%M %p").lstrip("0")))
    if charter.return_trip:
        rows.append(("Return trip", "Yes"))
    if charter.quoted_amount_kobo is not None:
        rows.append(("Quoted price", naira(charter.quoted_amount_kobo)))
    return rows


async def _send(charter: CharterRequest, subject: str, heading: str, body: str, **extra) -> None:
    if charter.contact_email:
        html = render_template(
            "charter_notice.html",
            subject=subject,
            heading=heading,
            body=body,
            charter=charter,
            details=_summary_rows(charter),
            **extra,
        )
        await send_email(charter.contact_email, subject, html)
    if charter.contact_phone:
        await send_sms(charter.contact_phone, f"Ecojindu Charter {charter.reference}: {body}")


async def _notify_requested(db: AsyncSession, charter: CharterRequest) -> None:  # noqa: ARG001
    await _send(
        charter,
        f"Charter request received · {charter.reference}",
        "We've got your charter request",
        "Thanks — our operations team will review it and send you a price within one "
        "working day. Nothing is confirmed and nothing is charged until you accept the quote.",
    )


async def _notify_quoted(db: AsyncSession, charter: CharterRequest) -> None:  # noqa: ARG001
    await _send(
        charter,
        f"Your charter quote · {naira(charter.quoted_amount_kobo)} · {charter.reference}",
        "Your charter quote is ready",
        f"We can run this charter for {naira(charter.quoted_amount_kobo)}. "
        "Use the link below to pay and confirm your vehicle.",
        action_url=f"{_web_base()}/charter/{charter.reference}",
        action_label="View and pay",
    )


async def _notify_confirmed(db: AsyncSession, charter: CharterRequest) -> None:  # noqa: ARG001
    await _send(
        charter,
        f"Charter confirmed · {charter.reference}",
        "Your charter is confirmed",
        "Payment received — the vehicle is yours for this trip. We'll confirm your "
        "driver and vehicle details shortly before travel.",
    )


async def _notify_cancelled(db: AsyncSession, charter: CharterRequest, reason: str) -> None:  # noqa: ARG001
    await _send(
        charter,
        f"Charter cancelled · {charter.reference}",
        "This charter has been cancelled",
        f"Reason: {reason}. Any payment will be refunded to your original method "
        "within 3–5 working days.",
    )


def _web_base() -> str:
    from app.core.config import settings

    return settings.WEB_BASE_URL
