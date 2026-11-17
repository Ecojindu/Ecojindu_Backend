"""Booking creation, lookup, cancellation and ticket resends."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import desc, select

from app.api.deps import CurrentUser, DbSession, OptionalUser, ServiceOrAdmin
from app.core.config import settings
from app.core.errors import ForbiddenError, NotFoundError
from app.core.ratelimit import RateLimiter
from app.models.booking import Booking
from app.models.enums import ADMIN_ROLES, BookingSource
from app.models.route import Route
from app.schemas.booking import (
    BookingCreate,
    BookingCreateResponse,
    BookingLookup,
    BookingOut,
    CancelBookingRequest,
    PaymentInit,
    InternalSubscriptionBooking,
    ResendTicketRequest,
    SubscriptionBookingCreate,
)
from app.schemas.common import Message
from app.services import bookings as booking_service
from app.services import payments as payment_service
from app.services.trips import get_trip_or_404, to_trip_out

router = APIRouter(prefix="/bookings", tags=["Bookings"])


async def _hydrate(db, booking: Booking) -> BookingOut:
    out = BookingOut.model_validate(booking)
    trip = await get_trip_or_404(db, booking.trip_id)
    route = await db.get(Route, trip.route_id)
    out.trip = to_trip_out(trip, route)
    return out


@router.post(
    "",
    response_model=BookingCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a booking and get a Paystack payment link",
    description=(
        "Holds the seats for the configured hold window (default 15 minutes) and returns "
        "everything the frontend needs to open Paystack's inline checkout. Guests do not "
        "need an account — only a name, phone and (optionally) an email address."
    ),
    dependencies=[Depends(RateLimiter("create_booking", limit=20))],
)
async def create_booking(
    payload: BookingCreate, db: DbSession, user: OptionalUser
) -> BookingCreateResponse:
    linked_user = user
    if linked_user is None and payload.passenger_email:
        linked_user = await booking_service.find_or_create_passenger(
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
        source=payload.source or BookingSource.WEB,
        user_id=linked_user.id if linked_user else None,
        pickup_stop_id=payload.pickup_stop_id,
        notes=payload.notes,
    )

    payment = await payment_service.initialize_booking_payment(db, booking, payload.passenger_email)

    # Test / sandbox path: Paystack is not live yet. Settle immediately so the
    # passenger lands on their QR ticket (email + SMS still fire as normal).
    if settings.PAYSTACK_MOCK and settings.PAYSTACK_AUTO_CONFIRM:
        await payment_service.simulate_successful_payment(db, payment.paystack_reference)
        await db.commit()
        await db.refresh(booking)
        return BookingCreateResponse(
            booking=await _hydrate(db, booking),
            payment=None,
            hold_expires_at=None,
            message=(
                "Test mode: payment skipped. Your ticket is confirmed — "
                "check email and SMS for the QR boarding pass."
            ),
        )

    await db.commit()
    await db.refresh(booking)

    return BookingCreateResponse(
        booking=await _hydrate(db, booking),
        payment=PaymentInit(
            reference=payment.paystack_reference,
            authorization_url=payment.authorization_url or "",
            access_code=payment.access_code or "",
            public_key=settings.PAYSTACK_PUBLIC_KEY,
            amount_kobo=payment.amount_kobo,
            email=payment.customer_email or "",
        ),
        hold_expires_at=booking.hold_expires_at,
        message=f"Seats held for {settings.SEAT_HOLD_MINUTES} minutes. Complete payment to confirm.",
    )


@router.post(
    "/subscription",
    response_model=BookingCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Book with ride credits (no payment)",
    description=(
        "For subscribers with an active plan. Deducts one credit per seat atomically, "
        "confirms the booking immediately and issues the QR ticket."
    ),
)
async def create_subscription_booking(
    payload: SubscriptionBookingCreate, db: DbSession, user: CurrentUser
) -> BookingCreateResponse:
    booking = await booking_service.create_subscription_booking(
        db,
        user=user,
        trip_id=payload.trip_id,
        seats=payload.seats,
        seats_male=payload.seats_male,
        seats_female=payload.seats_female,
        passenger_name=payload.passenger_name,
        passenger_phone=payload.passenger_phone,
        passenger_email=payload.passenger_email,
        pickup_stop_id=payload.pickup_stop_id,
    )
    await db.commit()
    await db.refresh(booking)
    return BookingCreateResponse(
        booking=await _hydrate(db, booking),
        payment=None,
        hold_expires_at=None,
        message=f"Confirmed with {payload.seats} ride credit(s). Your ticket is on its way.",
    )


@router.get("/mine", response_model=list[BookingOut], summary="My bookings")
async def my_bookings(
    db: DbSession,
    user: CurrentUser,
    upcoming_only: bool = Query(False, description="Only future, non-cancelled trips"),
) -> list[BookingOut]:
    from app.core.timeutil import now_utc
    from app.models.enums import BookingStatus
    from app.models.trip import Trip

    stmt = select(Booking).where(Booking.user_id == user.id)
    if upcoming_only:
        stmt = stmt.join(Trip, Trip.id == Booking.trip_id).where(
            Trip.departure_datetime >= now_utc(),
            Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN]),
        )
    stmt = stmt.order_by(desc(Booking.created_at)).limit(100)

    rows = list((await db.execute(stmt)).unique().scalars().all())
    return [await _hydrate(db, b) for b in rows]


@router.post(
    "/lookup",
    response_model=BookingOut,
    summary="Find a booking by reference + phone number (no login needed)",
)
async def lookup(payload: BookingLookup, db: DbSession) -> BookingOut:
    booking = await booking_service.lookup_booking(db, payload.booking_ref, payload.phone)
    return await _hydrate(db, booking)


@router.get("/{booking_ref}", response_model=BookingOut, summary="Booking by reference")
async def get_booking(booking_ref: str, db: DbSession, user: OptionalUser) -> BookingOut:
    booking = await booking_service.get_booking_by_ref(db, booking_ref)

    # A bare reference is not a credential: only the owner or staff see full details.
    if user is None or (user.id != booking.user_id and user.role not in {str(r) for r in ADMIN_ROLES}):
        raise ForbiddenError(
            "Use the lookup endpoint with your phone number to view this booking."
        )
    return await _hydrate(db, booking)


@router.post("/{booking_ref}/cancel", response_model=BookingOut, summary="Cancel a booking")
async def cancel_booking(
    booking_ref: str,
    payload: CancelBookingRequest,
    db: DbSession,
    user: OptionalUser,
    phone: str | None = Query(None, description="Required when cancelling as a guest"),
) -> BookingOut:
    booking = await booking_service.get_booking_by_ref(db, booking_ref)
    is_admin = user is not None and user.role in {str(r) for r in ADMIN_ROLES}

    if not is_admin:
        owns = user is not None and user.id == booking.user_id
        matches_phone = phone is not None and booking.passenger_phone == phone
        if not (owns or matches_phone):
            raise NotFoundError("We couldn't find a booking with those details.")

    await booking_service.cancel_booking(db, booking, reason=payload.reason, by_admin=is_admin)
    await db.commit()
    await db.refresh(booking)
    return await _hydrate(db, booking)


@router.post(
    "/{booking_ref}/resend-ticket",
    response_model=Message,
    summary="Resend the QR ticket by email and/or SMS",
    dependencies=[Depends(RateLimiter("resend", limit=6))],
)
async def resend_ticket(
    booking_ref: str,
    payload: ResendTicketRequest,
    db: DbSession,
    user: OptionalUser,
    phone: str | None = Query(None, description="Required when requesting as a guest"),
) -> Message:
    booking = await booking_service.get_booking_by_ref(db, booking_ref)
    is_admin = user is not None and user.role in {str(r) for r in ADMIN_ROLES}
    if not is_admin:
        owns = user is not None and user.id == booking.user_id
        if not (owns or (phone and booking.passenger_phone == phone)):
            raise NotFoundError("We couldn't find a booking with those details.")

    sent = await booking_service.resend_ticket(db, booking, payload.channels)
    await db.commit()
    return Message(message="Ticket resent to " + ", ".join(sent.values()) + ".")


@router.post(
    "/internal",
    response_model=BookingCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Service-to-service booking creation (used by ecojindu-api)",
    description="Requires the `X-Service-Key` header, or an admin bearer token.",
)
async def create_booking_internal(
    payload: BookingCreate, db: DbSession, caller: ServiceOrAdmin
) -> BookingCreateResponse:
    linked_user = None
    if payload.passenger_email or payload.passenger_phone:
        linked_user = await booking_service.find_or_create_passenger(
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
        source=payload.source or BookingSource.WHATSAPP,
        user_id=linked_user.id if linked_user else None,
        pickup_stop_id=payload.pickup_stop_id,
        notes=payload.notes,
    )
    payment = await payment_service.initialize_booking_payment(db, booking, payload.passenger_email)
    await db.commit()
    await db.refresh(booking)

    return BookingCreateResponse(
        booking=await _hydrate(db, booking),
        payment=PaymentInit(
            reference=payment.paystack_reference,
            authorization_url=payment.authorization_url or "",
            access_code=payment.access_code or "",
            public_key=settings.PAYSTACK_PUBLIC_KEY,
            amount_kobo=payment.amount_kobo,
            email=payment.customer_email or "",
        ),
        hold_expires_at=booking.hold_expires_at,
        message=f"Seats held for {settings.SEAT_HOLD_MINUTES} minutes.",
    )


@router.post(
    "/internal/subscription",
    response_model=BookingCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Service-to-service credit booking, identified by phone number",
    description=(
        "Lets the WhatsApp bot and the AI agent book on behalf of a subscriber without "
        "a passenger JWT. The phone number must belong to an account with active credits."
    ),
)
async def create_subscription_booking_internal(
    payload: InternalSubscriptionBooking, db: DbSession, caller: ServiceOrAdmin
) -> BookingCreateResponse:
    from app.models.user import User

    user = (
        await db.execute(select(User).where(User.phone == payload.phone))
    ).unique().scalar_one_or_none()
    if user is None:
        raise NotFoundError("No account is registered against that phone number.")

    booking = await booking_service.create_subscription_booking(
        db,
        user=user,
        trip_id=payload.trip_id,
        seats=payload.seats,
        seats_male=payload.seats_male,
        seats_female=payload.seats_female,
        passenger_name=payload.passenger_name,
        passenger_phone=payload.phone,
        passenger_email=payload.passenger_email,
        source=payload.source or BookingSource.SUBSCRIPTION,
    )
    await db.commit()
    await db.refresh(booking)
    return BookingCreateResponse(
        booking=await _hydrate(db, booking),
        payment=None,
        hold_expires_at=None,
        message=f"Confirmed with {payload.seats} ride credit(s).",
    )


class UpdateEmailRequest(BaseModel):
    email: EmailStr


@router.get(
    "/internal/active-for-phone",
    response_model=dict,
    summary="Get latest active or pending booking for a given phone number",
)
async def get_active_booking_for_phone(
    phone: str = Query(..., description="Customer phone number"),
    db: DbSession = None,
    caller: ServiceOrAdmin = None,
) -> dict:
    """Retrieve the most relevant active (pending_payment or confirmed) booking for a phone.

    Used by Ecojindu AI bot to maintain instant context across multi-turn conversations.
    """
    from app.models.enums import BookingStatus
    from app.models.payment import Payment

    # Normalize phone: extract last 10 digits
    clean_digits = "".join(c for c in phone if c.isdigit())
    last10 = clean_digits[-10:] if len(clean_digits) >= 10 else clean_digits

    stmt = (
        select(Booking)
        .where(
            Booking.passenger_phone.like(f"%{last10}%"),
            Booking.status.in_([BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN]),
        )
        .order_by(desc(Booking.created_at))
        .limit(1)
    )

    booking = (await db.execute(stmt)).unique().scalar_one_or_none()
    if not booking:
        return {"found": False, "booking": None}

    hydrated = await _hydrate(db, booking)

    # Check latest payment record if pending_payment
    payment_url = None
    payment_ref = None
    if booking.status == BookingStatus.PENDING_PAYMENT:
        pay_stmt = (
            select(Payment)
            .where(Payment.booking_id == booking.id)
            .order_by(desc(Payment.created_at))
            .limit(1)
        )
        payment = (await db.execute(pay_stmt)).scalar_one_or_none()
        if payment:
            payment_url = payment.authorization_url
            payment_ref = payment.paystack_reference

    qr_url = booking.ticket.qr_image_url if booking.ticket else None

    return {
        "found": True,
        "booking": {
            "booking_ref": booking.booking_ref,
            "passenger_name": booking.passenger_name,
            "passenger_phone": booking.passenger_phone,
            "passenger_email": booking.passenger_email,
            "status": booking.status.value if hasattr(booking.status, "value") else str(booking.status),
            "seats": booking.seats,
            "seat_numbers": booking.seat_numbers or [],
            "amount_kobo": booking.amount_kobo,
            "total_fare_naira": booking.amount_kobo // 100,
            "payment_url": payment_url,
            "payment_reference": payment_ref,
            "qr_image_url": qr_url,
            "created_at": booking.created_at.isoformat() if booking.created_at else None,
            "trip": {
                "id": str(hydrated.trip.id) if hydrated.trip else None,
                "route_name": hydrated.trip.route.name if hydrated.trip and hydrated.trip.route else None,
                "origin": hydrated.trip.origin_terminal if hydrated.trip else None,
                "destination": hydrated.trip.destination if hydrated.trip else None,
                "departure_datetime": hydrated.trip.departure_datetime.isoformat() if hydrated.trip else None,
                "departure_time": hydrated.trip.departure_time if hydrated.trip else None,
                "service_date": hydrated.trip.service_date.isoformat() if hydrated.trip else None,
            } if hydrated.trip else None,
        },
    }


@router.patch(
    "/internal/{booking_ref}/email",
    response_model=dict,
    summary="Update customer email on an existing booking",
)
async def update_booking_email(
    booking_ref: str,
    payload: UpdateEmailRequest,
    db: DbSession,
    caller: ServiceOrAdmin,
) -> dict:
    booking = await booking_service.get_booking_by_ref(db, booking_ref)
    booking.passenger_email = payload.email.strip()
    if booking.user:
        booking.user.email = payload.email.strip()
    await db.commit()
    return {
        "success": True,
        "booking_ref": booking.booking_ref,
        "passenger_email": booking.passenger_email,
    }


@router.get(
    "/internal/{booking_ref}",
    response_model=BookingOut,
    summary="Service-to-service booking lookup",
)
async def get_booking_internal(booking_ref: str, db: DbSession, caller: ServiceOrAdmin) -> BookingOut:
    booking = await booking_service.get_booking_by_ref(db, booking_ref)
    return await _hydrate(db, booking)


@router.post(
    "/internal/{booking_ref}/cancel",
    response_model=BookingOut,
    summary="Service-to-service cancellation",
)
async def cancel_booking_internal(
    booking_ref: str, payload: CancelBookingRequest, db: DbSession, caller: ServiceOrAdmin
) -> BookingOut:
    booking = await booking_service.get_booking_by_ref(db, booking_ref)
    await booking_service.cancel_booking(db, booking, reason=payload.reason, by_admin=True)
    await db.commit()
    await db.refresh(booking)
    return await _hydrate(db, booking)


@router.post(
    "/internal/{booking_ref}/resend-ticket",
    response_model=Message,
    summary="Service-to-service ticket resend",
)
async def resend_ticket_internal(
    booking_ref: str, payload: ResendTicketRequest, db: DbSession, caller: ServiceOrAdmin
) -> Message:
    booking = await booking_service.get_booking_by_ref(db, booking_ref)
    sent = await booking_service.resend_ticket(db, booking, payload.channels)
    await db.commit()
    return Message(message="Ticket resent to " + ", ".join(sent.values()) + ".")
