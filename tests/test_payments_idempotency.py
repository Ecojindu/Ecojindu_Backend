"""Paystack webhook processing must converge on the same state however often it fires."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.models.booking import Booking, Ticket
from app.models.enums import BookingStatus, PaymentStatus, SubscriptionStatus
from app.models.payment import Payment, WebhookEvent
from app.models.subscription import Subscription
from app.models.trip import Trip
from app.services.bookings import create_booking
from app.services.payments import process_paystack_event


def _charge_success(reference: str, amount_kobo: int) -> dict:
    return {
        "event": "charge.success",
        "data": {
            "reference": reference,
            "amount": amount_kobo,
            "currency": "NGN",
            "channel": "card",
            "status": "success",
            "customer": {"email": "passenger@example.com"},
        },
    }


async def _pending_booking_payment(db, trip, seats=2) -> tuple[Booking, Payment]:
    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Ifeanyi Duru",
        passenger_phone="+2348123334455", passenger_email="ifeanyi@example.com", seats=seats,
    )
    payment = Payment(
        booking_id=booking.id,
        paystack_reference=f"EJSBK-{booking.booking_ref}",
        amount_kobo=booking.amount_kobo,
        status=PaymentStatus.PENDING,
        customer_email="ifeanyi@example.com",
    )
    db.add(payment)
    await db.commit()
    return booking, payment


async def test_charge_success_confirms_and_issues_ticket(db, trip):
    booking, payment = await _pending_booking_payment(db, trip)

    result = await process_paystack_event(db, _charge_success(payment.paystack_reference, payment.amount_kobo))
    await db.commit()

    assert result["status"] == "processed"

    booking = (
        await db.execute(select(Booking).where(Booking.id == booking.id))
    ).unique().scalar_one()
    assert booking.status == BookingStatus.CONFIRMED
    assert booking.confirmed_at is not None
    assert booking.hold_expires_at is None

    ticket = (
        await db.execute(select(Ticket).where(Ticket.booking_id == booking.id))
    ).scalar_one()
    assert ticket.qr_token.startswith("EJS1.")


async def test_duplicate_delivery_is_ignored(db, trip):
    booking, payment = await _pending_booking_payment(db, trip)
    body = _charge_success(payment.paystack_reference, payment.amount_kobo)

    first = await process_paystack_event(db, body)
    await db.commit()
    second = await process_paystack_event(db, body)
    await db.commit()
    third = await process_paystack_event(db, body)
    await db.commit()

    assert first["status"] == "processed"
    assert second["status"] == "duplicate"
    assert third["status"] == "duplicate"

    # Exactly one ledger row, one ticket, and the seat count never double-counted.
    events = (
        await db.execute(
            select(func.count()).select_from(WebhookEvent).where(
                WebhookEvent.event_key == f"paystack:charge.success:{payment.paystack_reference}"
            )
        )
    ).scalar_one()
    assert events == 1

    tickets = (
        await db.execute(select(func.count()).select_from(Ticket).where(Ticket.booking_id == booking.id))
    ).scalar_one()
    assert tickets == 1

    refreshed_trip = await db.get(Trip, trip.id, populate_existing=True)
    assert refreshed_trip.seats_booked == booking.seats


async def test_concurrent_duplicate_deliveries_are_safe(session_factory, trip):
    """Paystack retries can overlap — only one delivery may do the work."""
    async with session_factory() as db:
        booking, payment = await _pending_booking_payment(db, trip, seats=1)
        reference, amount = payment.paystack_reference, payment.amount_kobo
        booking_id = booking.id

    async def deliver():
        async with session_factory() as session:
            try:
                out = await process_paystack_event(session, _charge_success(reference, amount))
                await session.commit()
                return out
            except Exception as exc:  # noqa: BLE001
                return exc

    results = await asyncio.gather(*[deliver() for _ in range(5)])
    processed = [r for r in results if isinstance(r, dict) and r.get("status") == "processed"]
    assert len(processed) == 1, f"exactly one delivery should do the work, got {results}"

    async with session_factory() as session:
        tickets = (
            await session.execute(
                select(func.count()).select_from(Ticket).where(Ticket.booking_id == booking_id)
            )
        ).scalar_one()
        assert tickets == 1


async def test_underpayment_is_rejected(db, trip):
    booking, payment = await _pending_booking_payment(db, trip)

    result = await process_paystack_event(
        db, _charge_success(payment.paystack_reference, payment.amount_kobo - 100_000)
    )
    await db.commit()

    assert result["status"] == "underpaid"
    booking = (
        await db.execute(select(Booking).where(Booking.id == booking.id))
    ).unique().scalar_one()
    assert booking.status == BookingStatus.PENDING_PAYMENT


async def test_unknown_reference_is_recorded_not_crashed(db):
    result = await process_paystack_event(db, _charge_success("EJSBK-NOT-A-REAL-REF", 100))
    await db.commit()
    assert result["status"] == "unknown_reference"


async def test_failed_then_success_still_settles(db, trip):
    booking, payment = await _pending_booking_payment(db, trip)

    await process_paystack_event(
        db,
        {"event": "charge.failed", "data": {"reference": payment.paystack_reference, "amount": payment.amount_kobo}},
    )
    await db.commit()

    failed = await db.get(Payment, payment.id)
    assert failed.status == PaymentStatus.FAILED

    await process_paystack_event(db, _charge_success(payment.paystack_reference, payment.amount_kobo))
    await db.commit()

    settled = await db.get(Payment, payment.id, populate_existing=True)
    assert settled.status == PaymentStatus.SUCCESS


async def test_subscription_payment_activates_credits(db, passenger, plan):
    subscription = Subscription(
        user_id=passenger.id,
        plan_id=plan.id,
        credits_total=plan.ride_credits,
        credits_used=0,
        status=SubscriptionStatus.PENDING_PAYMENT,
    )
    db.add(subscription)
    await db.flush()

    payment = Payment(
        subscription_id=subscription.id,
        paystack_reference=f"EJSSUB-{subscription.id.hex[:10].upper()}",
        amount_kobo=plan.price_kobo,
        status=PaymentStatus.PENDING,
        customer_email=passenger.email,
    )
    db.add(payment)
    await db.commit()

    await process_paystack_event(db, _charge_success(payment.paystack_reference, plan.price_kobo))
    await db.commit()

    activated = (
        await db.execute(select(Subscription).where(Subscription.id == subscription.id))
    ).unique().scalar_one()
    assert activated.status == SubscriptionStatus.ACTIVE
    assert activated.credits_total == plan.ride_credits
    assert activated.expires_at is not None
    assert activated.amount_paid_kobo == plan.price_kobo
