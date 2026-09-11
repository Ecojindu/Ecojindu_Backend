"""Ride credits must be spendable exactly once, even under a race."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models.booking import Booking
from app.models.enums import BookingStatus, SubscriptionStatus
from app.models.subscription import Subscription
from app.models.trip import Trip
from app.models.user import User
from app.services.bookings import (
    active_subscription_for_user,
    cancel_booking,
    create_subscription_booking,
    deduct_credits_atomic,
)


async def test_credit_booking_is_free_and_instantly_confirmed(db, trip, passenger, subscription):
    booking = await create_subscription_booking(
        db, user=passenger, trip_id=trip.id, seats=1
    )
    await db.commit()

    assert booking.amount_kobo == 0
    assert booking.status == BookingStatus.CONFIRMED
    assert booking.subscription_id == subscription.id
    assert booking.confirmed_at is not None

    refreshed = (
        await db.execute(select(Subscription).where(Subscription.id == subscription.id))
    ).unique().scalar_one()
    assert refreshed.credits_used == 1
    assert refreshed.credits_remaining == 1

    # And the ticket exists.
    await db.refresh(booking, ["ticket"])
    assert booking.ticket is not None
    assert booking.ticket.qr_token.startswith("EJS1.")


async def test_booking_more_seats_than_credits_is_rejected(db, trip, passenger, subscription):
    with pytest.raises(ConflictError) as exc:
        await create_subscription_booking(db, user=passenger, trip_id=trip.id, seats=3)
    assert exc.value.code == "insufficient_credits"

    refreshed = await db.get(Subscription, subscription.id, populate_existing=True)
    assert refreshed.credits_used == 0


async def test_credits_exhaust_and_flip_status(db, trip, passenger, subscription):
    await create_subscription_booking(db, user=passenger, trip_id=trip.id, seats=2)
    await db.commit()

    refreshed = (
        await db.execute(select(Subscription).where(Subscription.id == subscription.id))
    ).unique().scalar_one()
    assert refreshed.credits_used == 2
    assert refreshed.credits_remaining == 0
    assert refreshed.status == SubscriptionStatus.EXHAUSTED

    assert await active_subscription_for_user(db, passenger.id) is None


async def test_concurrent_credit_spends_never_exceed_balance(session_factory, subscription):
    """Five racing deductions against a 2-credit balance: exactly 2 succeed."""
    subscription_id = subscription.id

    async def spend():
        async with session_factory() as session:
            ok = await deduct_credits_atomic(session, subscription_id, 1)
            await session.commit()
            return ok

    results = await asyncio.gather(*[spend() for _ in range(5)])
    assert sum(1 for r in results if r) == 2

    async with session_factory() as session:
        refreshed = await session.get(Subscription, subscription_id)
        assert refreshed.credits_used == 2
        assert refreshed.credits_used <= refreshed.credits_total


async def test_concurrent_credit_bookings_never_oversell_credits(
    session_factory, trip, passenger, subscription
):
    """The full booking path, not just the UPDATE — seats and credits must agree."""
    trip_id, subscription_id, user_id = trip.id, subscription.id, passenger.id

    async def book():
        async with session_factory() as session:
            user = await session.get(User, user_id)
            try:
                b = await create_subscription_booking(session, user=user, trip_id=trip_id, seats=1)
                await session.commit()
                return b.booking_ref
            except Exception as exc:  # noqa: BLE001 - losers report whatever stopped them
                await session.rollback()
                return exc

    results = await asyncio.gather(*[book() for _ in range(4)])
    successes = [r for r in results if isinstance(r, str)]
    assert len(successes) == 2, f"only 2 credits exist, got {results}"

    async with session_factory() as session:
        sub = await session.get(Subscription, subscription_id)
        assert sub.credits_used == 2
        trip_row = await session.get(Trip, trip_id)
        assert trip_row.seats_booked == 2


async def test_cancelling_a_credit_booking_refunds_the_credit(db, trip, passenger, subscription):
    booking = await create_subscription_booking(db, user=passenger, trip_id=trip.id, seats=1)
    await db.commit()

    await cancel_booking(db, booking, reason="Change of plan", by_admin=True)
    await db.commit()

    refreshed = (
        await db.execute(select(Subscription).where(Subscription.id == subscription.id))
    ).unique().scalar_one()
    assert refreshed.credits_used == 0
    assert refreshed.status == SubscriptionStatus.ACTIVE

    released = await db.get(Trip, trip.id, populate_existing=True)
    assert released.seats_booked == 0

    cancelled = (
        await db.execute(select(Booking).where(Booking.id == booking.id))
    ).unique().scalar_one()
    assert cancelled.status == BookingStatus.CANCELLED
