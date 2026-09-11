"""Passenger sex counts — collected for state reporting, never fabricated."""
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, text

from app.models.booking import Booking
from app.schemas.booking import BookingCreate
from app.services.analytics import demographics
from app.services.bookings import confirm_booking, create_booking


def _payload(**overrides):
    base = {
        "trip_id": "3f2c8d1a-0000-4000-8000-000000000001",
        "passenger_name": "Ngozi Eze",
        "passenger_phone": "08055512345",
        "seats": 3,
    }
    return {**base, **overrides}


def test_counts_must_account_for_every_seat():
    with pytest.raises(PydanticValidationError) as exc:
        BookingCreate(**_payload(seats_male=1, seats_female=1))
    assert "doesn't match 3 seat(s)" in str(exc.value)


def test_matching_counts_validate():
    payload = BookingCreate(**_payload(seats_male=1, seats_female=2))
    assert payload.seats_male + payload.seats_female == payload.seats


def test_counts_may_be_omitted_entirely():
    """Channels that genuinely don't know — the WhatsApp bot — record nothing."""
    payload = BookingCreate(**_payload())
    assert payload.seats_male == 0
    assert payload.seats_female == 0


def test_all_of_one_sex_is_valid():
    payload = BookingCreate(**_payload(seats_male=3, seats_female=0))
    assert payload.seats_male == 3


async def test_counts_persist_on_the_booking(db, trip):
    booking = await create_booking(
        db,
        trip_id=trip.id,
        passenger_name="Ngozi Eze",
        passenger_phone="+2348055512345",
        passenger_email=None,
        seats=3,
        seats_male=1,
        seats_female=2,
    )
    await db.commit()

    stored = (
        await db.execute(select(Booking).where(Booking.id == booking.id))
    ).unique().scalar_one()
    assert (stored.seats_male, stored.seats_female) == (1, 2)


async def test_database_rejects_a_mismatched_split(db, trip):
    """Belt and braces: the API validates, and so does the database."""
    booking = await create_booking(
        db,
        trip_id=trip.id,
        passenger_name="Ngozi Eze",
        passenger_phone="+2348055512346",
        passenger_email=None,
        seats=2,
        seats_male=1,
        seats_female=1,
    )
    await db.commit()

    with pytest.raises(Exception) as exc:
        await db.execute(
            text("UPDATE bookings SET seats_male = 5 WHERE id = :id"), {"id": booking.id}
        )
        await db.commit()
    assert "seats_by_sex_match_total" in str(exc.value)
    await db.rollback()


async def test_unrecorded_bookings_count_as_unspecified_not_dropped(db, trip):
    """Coverage must stay honest — legacy bookings can't quietly leave the denominator."""
    from app.core.timeutil import today_lagos

    recorded = await create_booking(
        db, trip_id=trip.id, passenger_name="With Sex", passenger_phone="+2348055512347",
        passenger_email=None, seats=2, seats_male=2, seats_female=0,
    )
    unrecorded = await create_booking(
        db, trip_id=trip.id, passenger_name="Without Sex", passenger_phone="+2348055512348",
        passenger_email=None, seats=2,
    )
    await confirm_booking(db, recorded, send_notifications=False)
    await confirm_booking(db, unrecorded, send_notifications=False)
    await db.commit()

    today = today_lagos()
    stats = await demographics(db, start=today, end=today)

    assert stats["male"] == 2
    assert stats["unspecified"] == 2
    assert stats["total_seats"] == 4
    # 2 of 4 seats have a sex recorded — reported as such, not as 100%.
    assert stats["coverage_pct"] == 50.0
    assert stats["male_pct"] == 100.0
