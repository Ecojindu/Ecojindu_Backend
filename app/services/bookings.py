"""Booking lifecycle: hold → pay → confirm → check in → complete.

Two invariants matter most here and both are enforced in the database rather
than in Python:

* **Seats can never be oversold.** Every mutation of `trips.seats_booked`
  happens while holding a `SELECT … FOR UPDATE` lock on the trip row.
* **A ride credit can never be spent twice.** Credits are deducted with a
  conditional UPDATE whose WHERE clause re-checks the balance; a losing
  concurrent request updates zero rows and is rejected.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, SeatUnavailableError, ValidationError
from app.core.logging import log_event
from app.core.security import generate_booking_ref
from app.core.timeutil import now_utc
from app.models.booking import Booking, Ticket
from app.models.enums import BookingSource, BookingStatus, SubscriptionStatus, TripStatus
from app.models.route import Route
from app.models.subscription import Subscription
from app.models.trip import Trip
from app.models.user import User
from app.services import notifications
from app.services.tickets import issue_ticket
from app.services.trips import allocate_seat_numbers, lock_trip, release_expired_holds

logger = logging.getLogger("ecojindu.bookings")

#: How close to departure we still accept a new booking.
BOOKING_CUTOFF_MINUTES = 10
#: Free cancellation window.
CANCELLATION_CUTOFF_HOURS = 2


async def _unique_booking_ref(db: AsyncSession) -> str:
    for _ in range(12):
        ref = generate_booking_ref()
        exists = (
            await db.execute(select(Booking.id).where(Booking.booking_ref == ref))
        ).scalar_one_or_none()
        if exists is None:
            return ref
    raise ConflictError("Could not allocate a booking reference. Please try again.")


def assert_trip_bookable(trip: Trip, seats: int) -> None:
    if trip.status == TripStatus.CANCELLED:
        raise ConflictError("That departure has been cancelled.")
    if trip.status in {TripStatus.DEPARTED, TripStatus.ARRIVED}:
        raise ConflictError("That shuttle has already left the terminal.")
    if trip.departure_datetime <= now_utc() + timedelta(minutes=BOOKING_CUTOFF_MINUTES):
        raise ConflictError(
            f"Bookings close {BOOKING_CUTOFF_MINUTES} minutes before departure. "
            "Please pick a later departure."
        )
    if trip.seats_available < seats:
        raise SeatUnavailableError(
            f"Only {trip.seats_available} seat(s) left on that departure.",
            details={"seats_available": trip.seats_available, "seats_requested": seats},
        )


async def create_booking(
    db: AsyncSession,
    *,
    trip_id: uuid.UUID,
    passenger_name: str,
    passenger_phone: str,
    passenger_email: str | None,
    seats: int,
    source: str = BookingSource.WEB,
    user_id: uuid.UUID | None = None,
    pickup_stop_id: uuid.UUID | None = None,
    notes: str | None = None,
    amount_kobo_override: int | None = None,
    seats_male: int = 0,
    seats_female: int = 0,
    payment_method: str | None = None,
    payment_reference: str | None = None,
) -> Booking:
    """Reserve seats and open a payment hold. Does not take payment."""
    await release_expired_holds(db)

    # Anti-hoarding: prevent more than 2 concurrent unpaid active holds per phone number
    active_holds_count = (
        await db.execute(
            select(func.count(Booking.id)).where(
                Booking.passenger_phone == passenger_phone,
                Booking.status == BookingStatus.PENDING_PAYMENT,
                Booking.hold_expires_at > now_utc(),
            )
        )
    ).scalar_one()
    if active_holds_count >= 2:
        raise ConflictError(
            "You already have active unpaid bookings on hold. Please complete payment or wait for them to expire."
        )

    trip = await lock_trip(db, trip_id)
    assert_trip_bookable(trip, seats)

    seat_numbers = await allocate_seat_numbers(db, trip, seats)
    fare = trip.fare_kobo if amount_kobo_override is None else amount_kobo_override
    amount = fare * seats if amount_kobo_override is None else amount_kobo_override

    booking = Booking(
        booking_ref=await _unique_booking_ref(db),
        user_id=user_id,
        trip_id=trip.id,
        passenger_name=passenger_name.strip(),
        passenger_phone=passenger_phone,
        passenger_email=(passenger_email or None),
        seats=seats,
        seats_male=seats_male,
        seats_female=seats_female,
        seat_numbers=seat_numbers,
        amount_kobo=amount,
        source=source,
        status=BookingStatus.PENDING_PAYMENT,
        hold_expires_at=now_utc() + timedelta(minutes=settings.SEAT_HOLD_MINUTES),
        pickup_stop_id=pickup_stop_id,
        notes=notes,
        payment_method=payment_method,
        payment_reference=payment_reference,
    )
    db.add(booking)

    # The seats are consumed immediately — that is what makes the hold real.
    trip.seats_booked += seats
    await db.flush()

    log_event(
        logger,
        logging.INFO,
        "booking held",
        ref=booking.booking_ref,
        trip=str(trip.id),
        seats=seats,
        source=source,
    )
    return booking


async def confirm_booking(
    db: AsyncSession, booking: Booking, *, send_notifications: bool = True
) -> Booking:
    """Move a held booking to confirmed, issue its ticket, and notify.

    Idempotent — calling it on an already-confirmed booking re-issues nothing
    and sends nothing, so duplicate webhook deliveries are harmless.
    """
    if booking.status in {BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN, BookingStatus.COMPLETED}:
        return booking
    if booking.status == BookingStatus.CANCELLED:
        # The hold lapsed before payment landed; re-take the seats if we still can.
        trip = await lock_trip(db, booking.trip_id)
        if trip.seats_available < booking.seats:
            raise SeatUnavailableError(
                "Payment arrived after the seat hold expired and the seats are now gone. "
                "A refund is due."
            )
        trip.seats_booked += booking.seats
        booking.cancelled_at = None
        booking.cancellation_reason = None

    booking.status = BookingStatus.CONFIRMED
    booking.confirmed_at = now_utc()
    booking.hold_expires_at = None
    await db.flush()

    trip = await db.get(Trip, booking.trip_id)
    route = await db.get(Route, trip.route_id)
    ticket = await issue_ticket(db, booking, trip)

    if send_notifications:
        try:
            await notifications.send_booking_confirmation(db, booking, trip, route, ticket)
        except Exception:  # noqa: BLE001 - a notification failure must not undo a paid booking
            logger.exception("confirmation notifications failed for %s", booking.booking_ref)
        try:
            from app.services.reminders import schedule_reminders

            await schedule_reminders(db, booking, trip)
        except Exception:  # noqa: BLE001
            logger.exception("reminder scheduling failed for %s", booking.booking_ref)

    log_event(logger, logging.INFO, "booking confirmed", ref=booking.booking_ref)
    return booking


async def cancel_booking(
    db: AsyncSession,
    booking: Booking,
    *,
    reason: str | None = None,
    by_admin: bool = False,
    refund_credit: bool = True,
) -> Booking:
    if booking.status == BookingStatus.CANCELLED:
        return booking
    if booking.status in {BookingStatus.CHECKED_IN, BookingStatus.COMPLETED}:
        raise ConflictError("This booking has already been travelled and cannot be cancelled.")

    trip = await lock_trip(db, booking.trip_id)

    if not by_admin:
        cutoff = trip.departure_datetime - timedelta(hours=CANCELLATION_CUTOFF_HOURS)
        if now_utc() > cutoff:
            raise ConflictError(
                f"Bookings can only be cancelled up to {CANCELLATION_CUTOFF_HOURS} hours before "
                "departure. Please call us on " + settings.COMPANY_PHONE + "."
            )

    trip.seats_booked = max(trip.seats_booked - booking.seats, 0)
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = now_utc()
    booking.cancellation_reason = reason or ("Cancelled by operations" if by_admin else "Cancelled by passenger")

    # Subscription bookings return the credit to the passenger's balance.
    if refund_credit and booking.subscription_id:
        await db.execute(
            update(Subscription)
            .where(and_(Subscription.id == booking.subscription_id, Subscription.credits_used >= booking.seats))
            .values(credits_used=Subscription.credits_used - booking.seats, status=SubscriptionStatus.ACTIVE)
            .execution_options(synchronize_session="fetch")
        )

    await db.flush()
    log_event(logger, logging.INFO, "booking cancelled", ref=booking.booking_ref, by_admin=by_admin)
    return booking


async def reschedule_booking(
    db: AsyncSession,
    booking: Booking,
    new_trip_id: uuid.UUID,
    *,
    reason: str,
    new_seat_numbers: list[str] | None = None,
) -> Booking:
    if booking.status in {BookingStatus.CANCELLED, BookingStatus.REFUNDED, BookingStatus.NO_SHOW}:
        raise ConflictError(f"Cannot reschedule a booking with status '{booking.status}'.")
    if booking.status in {BookingStatus.CHECKED_IN, BookingStatus.COMPLETED}:
        raise ConflictError("This booking has already been travelled and cannot be rescheduled.")

    old_trip = await lock_trip(db, booking.trip_id)
    new_trip = await lock_trip(db, new_trip_id)

    assert_trip_bookable(new_trip, booking.seats)

    # Free seats on old trip
    old_trip.seats_booked = max(old_trip.seats_booked - booking.seats, 0)

    # Assign seats on new trip
    if new_seat_numbers and len(new_seat_numbers) == booking.seats:
        seats_assigned = new_seat_numbers
    else:
        seats_assigned = await allocate_seat_numbers(db, new_trip, booking.seats)

    new_trip.seats_booked += booking.seats

    old_trip_id = booking.trip_id
    booking.trip_id = new_trip.id
    booking.seat_numbers = seats_assigned
    booking.notes = (booking.notes or "") + f" [Rescheduled from trip {old_trip_id}: {reason}]"

    # Re-issue ticket for the new trip if confirmed
    if booking.status in {BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN}:
        await issue_ticket(db, booking, new_trip)

    await db.flush()
    log_event(logger, logging.INFO, "booking rescheduled", ref=booking.booking_ref, new_trip=str(new_trip.id))
    return booking


async def refund_booking(
    db: AsyncSession,
    booking: Booking,
    *,
    amount_kobo: int | None = None,
    reason: str,
    refund_method: str = "paystack",
) -> Booking:
    if booking.status == BookingStatus.REFUNDED:
        return booking

    if booking.status in {BookingStatus.CHECKED_IN, BookingStatus.COMPLETED}:
        raise ConflictError("Cannot refund a booking that has already been boarded or completed.")

    # Free seats if currently holding seats
    if booking.status in {BookingStatus.CONFIRMED, BookingStatus.PENDING_PAYMENT}:
        trip = await lock_trip(db, booking.trip_id)
        trip.seats_booked = max(trip.seats_booked - booking.seats, 0)

    refund_amt = amount_kobo if amount_kobo is not None else booking.amount_kobo
    booking.status = BookingStatus.REFUNDED
    booking.cancelled_at = now_utc()
    booking.cancellation_reason = f"Refunded via {refund_method} ({refund_amt / 100:.2f} NGN): {reason}"

    # If subscription booking, return credit
    if booking.subscription_id:
        await db.execute(
            update(Subscription)
            .where(and_(Subscription.id == booking.subscription_id, Subscription.credits_used >= booking.seats))
            .values(credits_used=Subscription.credits_used - booking.seats, status=SubscriptionStatus.ACTIVE)
            .execution_options(synchronize_session="fetch")
        )

    await db.flush()
    log_event(logger, logging.INFO, "booking refunded", ref=booking.booking_ref, amount=refund_amt)
    return booking


async def mark_no_show(
    db: AsyncSession,
    booking: Booking,
    *,
    reason: str | None = None,
) -> Booking:
    if booking.status not in {BookingStatus.CONFIRMED}:
        raise ConflictError(f"Cannot mark booking with status '{booking.status}' as no-show.")

    booking.status = BookingStatus.NO_SHOW
    booking.notes = (booking.notes or "") + f" [Marked No-Show: {reason or 'Passenger did not arrive'}]"
    await db.flush()
    log_event(logger, logging.INFO, "booking marked no-show", ref=booking.booking_ref)
    return booking


# ── Subscription (credit) bookings ───────────────────────────


async def active_subscription_for_user(db: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(
            and_(
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.credits_used < Subscription.credits_total,
            )
        )
        .order_by(Subscription.expires_at.asc().nulls_last())
    )
    subs = list((await db.execute(stmt)).unique().scalars().all())
    now = now_utc()
    for sub in subs:
        if sub.expires_at and sub.expires_at < now:
            sub.status = SubscriptionStatus.EXPIRED
            continue
        return sub
    await db.flush()
    return None


async def deduct_credits_atomic(db: AsyncSession, subscription_id: uuid.UUID, credits: int) -> bool:
    """Spend `credits` if and only if that many remain. Returns success.

    The balance check lives inside the UPDATE's WHERE clause, so two concurrent
    requests for the last credit cannot both succeed regardless of isolation level.
    """
    result = await db.execute(
        update(Subscription)
        .where(
            and_(
                Subscription.id == subscription_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.credits_total - Subscription.credits_used >= credits,
            )
        )
        .values(credits_used=Subscription.credits_used + credits)
        # "fetch" keeps any already-loaded Subscription instance in this session
        # in step with the row the database just wrote.
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount != 1:
        return False

    # Flip to exhausted once the last credit is gone.
    await db.execute(
        update(Subscription)
        .where(
            and_(
                Subscription.id == subscription_id,
                Subscription.credits_used >= Subscription.credits_total,
            )
        )
        .values(status=SubscriptionStatus.EXHAUSTED)
        .execution_options(synchronize_session="fetch")
    )
    return True


async def create_subscription_booking(
    db: AsyncSession,
    *,
    user: User,
    trip_id: uuid.UUID,
    seats: int,
    passenger_name: str | None = None,
    passenger_phone: str | None = None,
    passenger_email: str | None = None,
    pickup_stop_id: uuid.UUID | None = None,
    source: str = BookingSource.SUBSCRIPTION,
    seats_male: int = 0,
    seats_female: int = 0,
) -> Booking:
    """Book with ride credits: zero payment, instant confirmation, ticket issued."""
    subscription = await active_subscription_for_user(db, user.id)
    if subscription is None:
        raise ConflictError(
            "You don't have an active subscription with ride credits.",
            code="no_active_subscription",
        )
    if subscription.credits_remaining < seats:
        raise ConflictError(
            f"You have {subscription.credits_remaining} ride credit(s) left but asked for {seats}.",
            code="insufficient_credits",
            details={"credits_remaining": subscription.credits_remaining},
        )

    await release_expired_holds(db)
    trip = await lock_trip(db, trip_id)
    assert_trip_bookable(trip, seats)

    if not await deduct_credits_atomic(db, subscription.id, seats):
        raise ConflictError(
            "Those ride credits were just used on another booking.",
            code="insufficient_credits",
        )

    seat_numbers = await allocate_seat_numbers(db, trip, seats)
    booking = Booking(
        booking_ref=await _unique_booking_ref(db),
        user_id=user.id,
        trip_id=trip.id,
        subscription_id=subscription.id,
        passenger_name=(passenger_name or user.full_name).strip(),
        passenger_phone=passenger_phone or user.phone,
        passenger_email=passenger_email or user.email,
        seats=seats,
        seats_male=seats_male,
        seats_female=seats_female,
        seat_numbers=seat_numbers,
        amount_kobo=0,
        source=source,
        status=BookingStatus.CONFIRMED,
        confirmed_at=now_utc(),
        hold_expires_at=None,
        pickup_stop_id=pickup_stop_id,
    )
    db.add(booking)
    trip.seats_booked += seats

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("That booking could not be completed. Please try again.") from None

    route = await db.get(Route, trip.route_id)
    ticket = await issue_ticket(db, booking, trip)
    try:
        await notifications.send_booking_confirmation(db, booking, trip, route, ticket)
    except Exception:  # noqa: BLE001
        logger.exception("confirmation notifications failed for %s", booking.booking_ref)

    log_event(
        logger,
        logging.INFO,
        "subscription booking confirmed",
        ref=booking.booking_ref,
        credits=seats,
        subscription=str(subscription.id),
    )
    return booking


# ── Lookups ──────────────────────────────────────────────────


async def get_booking_by_ref(db: AsyncSession, ref: str) -> Booking:
    normalised = ref.strip().upper()
    if not normalised.startswith("EJS-"):
        normalised = f"EJS-{normalised}"
    booking = (
        await db.execute(select(Booking).where(Booking.booking_ref == normalised))
    ).unique().scalar_one_or_none()
    if booking is None:
        raise NotFoundError("We couldn't find a booking with that reference.")
    return booking


async def lookup_booking(db: AsyncSession, ref: str, phone: str) -> Booking:
    booking = await get_booking_by_ref(db, ref)
    if booking.passenger_phone != phone:
        # Deliberately identical to the not-found message: a ref alone must not
        # confirm that a booking exists for someone else's phone number.
        raise NotFoundError("We couldn't find a booking with that reference and phone number.")
    return booking


async def get_ticket(db: AsyncSession, booking: Booking) -> Ticket | None:
    return (
        await db.execute(select(Ticket).where(Ticket.booking_id == booking.id))
    ).scalar_one_or_none()


async def resend_ticket(db: AsyncSession, booking: Booking, channels: list[str]) -> dict[str, str]:
    if booking.status not in {BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN}:
        raise ValidationError("Only confirmed bookings have a ticket to resend.")

    trip = await db.get(Trip, booking.trip_id)
    route = await db.get(Route, trip.route_id)
    ticket = await get_ticket(db, booking)
    if ticket is None:
        ticket = await issue_ticket(db, booking, trip)

    await notifications.send_booking_confirmation(db, booking, trip, route, ticket)

    sent = {}
    if "email" in channels:
        sent["email"] = booking.passenger_email or "no email on file"
    if "sms" in channels:
        sent["sms"] = booking.passenger_phone
    return sent


async def bookings_for_trip(db: AsyncSession, trip_id: uuid.UUID, *, active_only: bool = True) -> list[Booking]:
    stmt = select(Booking).where(Booking.trip_id == trip_id)
    if active_only:
        stmt = stmt.where(
            Booking.status.in_(
                [BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN, BookingStatus.COMPLETED]
            )
        )
    stmt = stmt.order_by(Booking.created_at.asc())
    return list((await db.execute(stmt)).unique().scalars().all())


async def find_or_create_passenger(
    db: AsyncSession, *, full_name: str, phone: str, email: str | None
) -> User:
    """Link a booking to an account when one exists, without forcing signup."""
    stmt = select(User).where(User.phone == phone)
    user = (await db.execute(stmt)).unique().scalar_one_or_none()
    if user:
        if email and not user.email:
            user.email = email
            await db.flush()
        return user

    if email:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).unique().scalar_one_or_none()
        if user:
            return user

    user = User(full_name=full_name.strip(), phone=phone, email=email, role="passenger")
    db.add(user)
    await db.flush()
    return user


def hold_seconds_remaining(booking: Booking, *, at: datetime | None = None) -> int:
    if not booking.hold_expires_at:
        return 0
    delta = booking.hold_expires_at - (at or now_utc())
    return max(int(delta.total_seconds()), 0)
