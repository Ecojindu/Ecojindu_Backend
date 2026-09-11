"""Payment orchestration and idempotent webhook processing.

Idempotency has two layers:

1. `webhook_events` holds a unique key per delivery (`paystack:<event>:<ref>`).
   The insert is committed before any business logic runs, so a duplicate
   delivery hits the unique index and returns early.
2. Every downstream action (`confirm_booking`, `activate_subscription`) is
   itself idempotent, so even a torn retry converges on the same state.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotFoundError, PaymentError
from app.core.logging import log_event
from app.core.security import generate_payment_reference
from app.core.timeutil import now_utc
from app.models.booking import Booking
from app.models.enums import BookingStatus, PaymentStatus, SubscriptionStatus
from app.models.payment import Payment, WebhookEvent
from app.models.subscription import Subscription
from app.models.user import User
from app.services import notifications
from app.services.bookings import confirm_booking
from app.services.paystack import paystack

logger = logging.getLogger("ecojindu.payments")


async def initialize_booking_payment(db: AsyncSession, booking: Booking, email: str | None) -> Payment:
    reference = generate_payment_reference("EJSBK")
    customer_email = email or booking.passenger_email or f"{booking.passenger_phone.lstrip('+')}@guest.ecojindu.ng"

    init = await paystack.initialize(
        email=customer_email,
        amount_kobo=booking.amount_kobo,
        reference=reference,
        metadata={
            "booking_ref": booking.booking_ref,
            "booking_id": str(booking.id),
            "trip_id": str(booking.trip_id),
            "seats": booking.seats,
            "kind": "booking",
        },
    )

    payment = Payment(
        booking_id=booking.id,
        paystack_reference=init.reference,
        authorization_url=init.authorization_url,
        access_code=init.access_code,
        amount_kobo=booking.amount_kobo,
        currency="NGN",
        status=PaymentStatus.PENDING,
        customer_email=customer_email,
    )
    db.add(payment)
    await db.flush()
    return payment


async def initialize_subscription_payment(
    db: AsyncSession, subscription: Subscription, email: str
) -> Payment:
    reference = generate_payment_reference("EJSSUB")
    init = await paystack.initialize(
        email=email,
        amount_kobo=subscription.plan.price_kobo,
        reference=reference,
        metadata={
            "subscription_id": str(subscription.id),
            "plan": subscription.plan.name,
            "kind": "subscription",
        },
    )
    payment = Payment(
        subscription_id=subscription.id,
        paystack_reference=init.reference,
        authorization_url=init.authorization_url,
        access_code=init.access_code,
        amount_kobo=subscription.plan.price_kobo,
        currency="NGN",
        status=PaymentStatus.PENDING,
        customer_email=email,
    )
    db.add(payment)
    await db.flush()
    return payment


async def initialize_charter_payment(db: AsyncSession, charter) -> Payment:
    """Payment link for a quoted charter. Reuses any live pending link.

    Re-quoting supersedes the old link, so a stale amount can never be paid.
    """
    if not charter.quoted_amount_kobo:
        raise PaymentError("This charter hasn't been quoted yet.")

    existing = (
        await db.execute(
            select(Payment).where(
                Payment.charter_request_id == charter.id,
                Payment.status == PaymentStatus.PENDING,
                Payment.amount_kobo == charter.quoted_amount_kobo,
            )
        )
    ).scalars().first()
    if existing:
        return existing

    email = charter.contact_email or f"{charter.contact_phone.lstrip('+')}@guest.ecojindu.ng"
    init = await paystack.initialize(
        email=email,
        amount_kobo=charter.quoted_amount_kobo,
        reference=generate_payment_reference("EJSCHT"),
        metadata={
            "charter_reference": charter.reference,
            "charter_id": str(charter.id),
            "passengers": charter.passengers,
            "kind": "charter",
        },
    )
    payment = Payment(
        charter_request_id=charter.id,
        paystack_reference=init.reference,
        authorization_url=init.authorization_url,
        access_code=init.access_code,
        amount_kobo=charter.quoted_amount_kobo,
        currency="NGN",
        status=PaymentStatus.PENDING,
        customer_email=email,
    )
    db.add(payment)
    await db.flush()
    return payment


async def get_payment_by_reference(db: AsyncSession, reference: str) -> Payment:
    payment = (
        await db.execute(select(Payment).where(Payment.paystack_reference == reference))
    ).scalar_one_or_none()
    if payment is None:
        raise NotFoundError("We have no record of that payment reference.")
    return payment


async def activate_subscription(db: AsyncSession, subscription: Subscription, amount_kobo: int) -> Subscription:
    """Idempotent — an already-active subscription is returned untouched."""
    if subscription.status == SubscriptionStatus.ACTIVE:
        return subscription

    plan = subscription.plan
    now = now_utc()
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.starts_at = subscription.starts_at or now
    subscription.expires_at = (subscription.starts_at or now) + timedelta(days=plan.validity_days)
    subscription.credits_total = subscription.credits_total or plan.ride_credits
    subscription.amount_paid_kobo = amount_kobo or plan.price_kobo
    await db.flush()

    user = await db.get(User, subscription.user_id)
    if user:
        try:
            await notifications.send_subscription_activated(db, subscription, plan, user)
        except Exception:  # noqa: BLE001
            logger.exception("subscription activation notifications failed")

    log_event(logger, logging.INFO, "subscription activated", subscription=str(subscription.id))
    return subscription


async def apply_successful_payment(db: AsyncSession, payment: Payment, *, raw: dict | None = None) -> Payment:
    """Settle a payment and fan out to whatever it paid for. Safe to re-run."""
    if payment.status == PaymentStatus.SUCCESS:
        log_event(logger, logging.INFO, "payment already settled", reference=payment.paystack_reference)
        return payment

    payment.status = PaymentStatus.SUCCESS
    payment.paid_at = payment.paid_at or now_utc()
    if raw:
        payment.raw_webhook = raw
        payment.channel = raw.get("channel") or payment.channel
    await db.flush()

    if payment.booking_id:
        booking = await db.get(Booking, payment.booking_id)
        if booking:
            await confirm_booking(db, booking)

    if payment.subscription_id:
        subscription = await db.get(Subscription, payment.subscription_id)
        if subscription:
            await activate_subscription(db, subscription, payment.amount_kobo)

    if payment.charter_request_id:
        from app.models.charter import CharterRequest
        from app.services import charter as charter_service

        charter = await db.get(CharterRequest, payment.charter_request_id)
        if charter:
            await charter_service.mark_confirmed(db, charter)

    return payment


async def mark_payment_failed(db: AsyncSession, payment: Payment, status: str, raw: dict | None = None) -> Payment:
    if payment.status == PaymentStatus.SUCCESS:
        return payment  # never downgrade a settled payment
    payment.status = status
    if raw:
        payment.raw_webhook = raw
    await db.flush()
    return payment


async def verify_and_settle(db: AsyncSession, reference: str) -> Payment:
    """Called by the frontend after Paystack's popup closes."""
    payment = await get_payment_by_reference(db, reference)
    if payment.status == PaymentStatus.SUCCESS:
        return payment

    result = await paystack.verify(reference)

    if result.status == "success":
        # Guard against a tampered client sending someone else's reference.
        if not paystack.mock and result.amount_kobo and result.amount_kobo < payment.amount_kobo:
            raise PaymentError("The amount paid is less than the amount due.")
        payment.channel = result.channel
        payment.paid_at = result.paid_at
        return await apply_successful_payment(db, payment, raw=result.raw)

    if result.status in {"failed", "abandoned"}:
        await mark_payment_failed(db, payment, result.status, result.raw)
    return payment


# ── Webhooks ─────────────────────────────────────────────────


async def record_webhook_event(
    db: AsyncSession, *, provider: str, event_key: str, event_type: str | None, payload: dict
) -> WebhookEvent | None:
    """Insert the idempotency row. Returns None when this delivery is a duplicate."""
    event = WebhookEvent(
        provider=provider,
        event_key=event_key,
        event_type=event_type,
        payload=payload,
        received_at=now_utc(),
    )
    try:
        # A SAVEPOINT, so a duplicate key rolls back only this insert. Rolling back
        # the whole session would discard unrelated work already done on it.
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        log_event(logger, logging.INFO, "duplicate webhook ignored", event_key=event_key)
        return None
    return event


async def process_paystack_event(db: AsyncSession, body: dict) -> dict:
    """Handle a verified Paystack webhook. Idempotent on repeated delivery."""
    event_type = body.get("event", "unknown")
    data = body.get("data") or {}
    reference = data.get("reference")

    if not reference:
        return {"status": "ignored", "reason": "no reference in payload"}

    event_key = f"paystack:{event_type}:{reference}"
    event = await record_webhook_event(
        db, provider="paystack", event_key=event_key, event_type=event_type, payload=body
    )
    if event is None:
        return {"status": "duplicate", "reference": reference}

    payment = (
        await db.execute(select(Payment).where(Payment.paystack_reference == reference))
    ).scalar_one_or_none()

    if payment is None:
        log_event(logger, logging.WARNING, "webhook for unknown reference", reference=reference)
        event.processed_at = now_utc()
        await db.flush()
        return {"status": "unknown_reference", "reference": reference}

    if event_type == "charge.success":
        amount = int(data.get("amount") or 0)
        if amount and amount < payment.amount_kobo:
            await mark_payment_failed(db, payment, PaymentStatus.FAILED, data)
            result = {"status": "underpaid", "reference": reference}
        else:
            payment.channel = data.get("channel")
            await apply_successful_payment(db, payment, raw=data)
            result = {"status": "processed", "reference": reference}
    elif event_type in {"charge.failed", "transaction.failed"}:
        await mark_payment_failed(db, payment, PaymentStatus.FAILED, data)
        result = {"status": "failed", "reference": reference}
    elif event_type in {"refund.processed", "charge.refunded"}:
        payment.status = PaymentStatus.REFUNDED
        payment.raw_webhook = data
        await db.flush()
        result = {"status": "refunded", "reference": reference}
    else:
        result = {"status": "ignored", "event": event_type}

    event.processed_at = now_utc()
    await db.flush()
    log_event(logger, logging.INFO, "paystack webhook processed", event=event_type, **result)
    return result


async def simulate_successful_payment(db: AsyncSession, reference: str) -> Payment:
    """Backs the local mock checkout page. Refuses to run outside mock mode."""
    if not (settings.PAYSTACK_MOCK or settings.ENVIRONMENT == "development"):
        raise PaymentError("Mock payments are disabled in this environment.")
    payment = await get_payment_by_reference(db, reference)
    payment.channel = "mock"
    return await apply_successful_payment(
        db, payment, raw={"mock": True, "reference": reference, "channel": "mock"}
    )


async def expire_stale_pending_payments(db: AsyncSession) -> int:
    """Housekeeping: abandon pending payments whose booking hold already lapsed."""
    cutoff = now_utc() - timedelta(hours=6)
    stmt = select(Payment).where(
        Payment.status == PaymentStatus.PENDING, Payment.created_at < cutoff
    )
    stale = list((await db.execute(stmt)).scalars().all())
    for payment in stale:
        payment.status = PaymentStatus.ABANDONED
    await db.flush()
    return len(stale)


async def revenue_for_booking_ids(db: AsyncSession, booking_ids: list[uuid.UUID]) -> int:
    if not booking_ids:
        return 0
    stmt = select(Booking.amount_kobo).where(
        Booking.id.in_(booking_ids),
        Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN, BookingStatus.COMPLETED]),
    )
    return sum((await db.execute(stmt)).scalars().all())
