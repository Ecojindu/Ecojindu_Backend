"""Seat-hold correctness: no overselling under concurrency, holds expire cleanly."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.errors import SeatUnavailableError
from app.core.timeutil import now_utc
from app.models.booking import Booking
from app.models.enums import BookingStatus
from app.models.trip import Trip
from app.services.bookings import create_booking
from app.services.trips import release_expired_holds


async def _book(session_factory, trip_id, name, seats):
    async with session_factory() as session:
        booking = await create_booking(
            session,
            trip_id=trip_id,
            passenger_name=name,
            passenger_phone=f"+234800000{abs(hash(name)) % 10000:04d}",
            passenger_email=None,
            seats=seats,
        )
        await session.commit()
        return booking.booking_ref


async def test_seat_hold_reserves_seats(db, trip):
    booking = await create_booking(
        db,
        trip_id=trip.id,
        passenger_name="Ifeanyi Duru",
        passenger_phone="+2348123334455",
        passenger_email="ifeanyi@example.com",
        seats=2,
    )
    await db.commit()

    refreshed = await db.get(Trip, trip.id, populate_existing=True)
    assert refreshed.seats_booked == 2
    assert refreshed.seats_available == 2
    assert booking.status == BookingStatus.PENDING_PAYMENT
    assert booking.hold_expires_at is not None
    assert booking.seat_numbers == ["1", "2"]


async def test_cannot_book_more_seats_than_remain(db, trip):
    await create_booking(
        db, trip_id=trip.id, passenger_name="A", passenger_phone="+2348100000001",
        passenger_email=None, seats=3,
    )
    await db.commit()

    with pytest.raises(SeatUnavailableError):
        await create_booking(
            db, trip_id=trip.id, passenger_name="B", passenger_phone="+2348100000002",
            passenger_email=None, seats=2,
        )


async def test_concurrent_bookings_never_oversell(session_factory, trip):
    """Six racing requests for 1 seat each on a 4-seat shuttle: exactly 4 win.

    Each request runs in its own session/transaction, so this exercises the real
    row-lock path rather than a single session's in-memory state.
    """
    results = await asyncio.gather(
        *[_book(session_factory, trip.id, f"racer-{i}", 1) for i in range(6)],
        return_exceptions=True,
    )

    succeeded = [r for r in results if isinstance(r, str)]
    rejected = [r for r in results if isinstance(r, Exception)]

    assert len(succeeded) == 4, f"expected exactly 4 winners, got {len(succeeded)}"
    assert len(rejected) == 2
    assert all(isinstance(r, SeatUnavailableError) for r in rejected)

    async with session_factory() as session:
        refreshed = await session.get(Trip, trip.id)
        assert refreshed.seats_booked == 4
        assert refreshed.seats_available == 0


async def test_concurrent_multi_seat_bookings_never_oversell(session_factory, trip):
    """Three racing requests for 2 seats each on 4 seats: exactly 2 win."""
    results = await asyncio.gather(
        *[_book(session_factory, trip.id, f"pair-{i}", 2) for i in range(3)],
        return_exceptions=True,
    )
    succeeded = [r for r in results if isinstance(r, str)]
    assert len(succeeded) == 2

    async with session_factory() as session:
        refreshed = await session.get(Trip, trip.id)
        assert refreshed.seats_booked == 4


async def test_expired_hold_releases_seats(db, trip):
    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Slow Payer", passenger_phone="+2348100000009",
        passenger_email=None, seats=3,
    )
    await db.commit()

    # Wind the hold back past its expiry.
    booking.hold_expires_at = now_utc() - timedelta(minutes=1)
    await db.commit()

    released = await release_expired_holds(db)
    await db.commit()

    assert released == 3
    refreshed = await db.get(Trip, trip.id, populate_existing=True)
    assert refreshed.seats_booked == 0

    stale = (
        await db.execute(select(Booking).where(Booking.id == booking.id))
    ).unique().scalar_one()
    assert stale.status == BookingStatus.CANCELLED
    assert "Payment not completed" in (stale.cancellation_reason or "")


async def test_confirmed_bookings_are_not_released(db, trip):
    from app.services.bookings import confirm_booking

    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Paid Up", passenger_phone="+2348100000010",
        passenger_email=None, seats=2,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()

    released = await release_expired_holds(db)
    assert released == 0

    refreshed = await db.get(Trip, trip.id, populate_existing=True)
    assert refreshed.seats_booked == 2
