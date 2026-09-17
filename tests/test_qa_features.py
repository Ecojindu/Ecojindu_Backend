"""Tests for QA improvements: reschedule, refund, no-show, hold limits, schedule conflict checks."""
from __future__ import annotations

import uuid
from datetime import timedelta
import pytest

from app.core.errors import ConflictError
from app.core.timeutil import now_utc, today_lagos
from app.models.booking import Booking
from app.models.enums import BookingStatus, TripStatus
from app.models.trip import Trip
from app.services.bookings import (
    cancel_booking,
    confirm_booking,
    create_booking,
    mark_no_show,
    refund_booking,
    reschedule_booking,
)
from app.api.v1.admin import assert_no_schedule_conflict


def _phone() -> str:
    return f"+23481{uuid.uuid4().int % 100_000_000:08d}"


async def test_hold_limit_prevents_hoarding(db, trip):
    phone = _phone()
    # 1st hold: OK
    b1 = await create_booking(
        db, trip_id=trip.id, passenger_name="Hold 1", passenger_phone=phone,
        passenger_email=None, seats=1,
    )
    # 2nd hold: OK
    b2 = await create_booking(
        db, trip_id=trip.id, passenger_name="Hold 2", passenger_phone=phone,
        passenger_email=None, seats=1,
    )

    # 3rd hold: Rejected
    with pytest.raises(ConflictError) as exc:
        await create_booking(
            db, trip_id=trip.id, passenger_name="Hold 3", passenger_phone=phone,
            passenger_email=None, seats=1,
        )
    assert "active unpaid bookings" in str(exc.value)


async def test_reschedule_booking_transfers_seats(db, route, vehicle):
    dep1 = now_utc() + timedelta(hours=3)
    t1 = Trip(
        route_id=route.id,
        service_date=today_lagos(),
        departure_datetime=dep1,
        arrival_estimate=dep1 + timedelta(minutes=60),
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=10,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    dep2 = now_utc() + timedelta(hours=5)
    t2 = Trip(
        route_id=route.id,
        service_date=today_lagos(),
        departure_datetime=dep2,
        arrival_estimate=dep2 + timedelta(minutes=60),
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=10,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add_all([t1, t2])
    await db.commit()

    booking = await create_booking(
        db, trip_id=t1.id, passenger_name="Rescheduler", passenger_phone=_phone(),
        passenger_email="reschedule@example.com", seats=2,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()
    await db.refresh(t1)
    await db.refresh(t2)
    assert t1.seats_booked == 2
    assert t2.seats_booked == 0

    # Reschedule to t2
    rescheduled = await reschedule_booking(db, booking, t2.id, reason="Flight delayed")
    await db.commit()
    await db.refresh(t1)
    await db.refresh(t2)

    assert rescheduled.trip_id == t2.id
    assert t1.seats_booked == 0
    assert t2.seats_booked == 2
    assert "Flight delayed" in rescheduled.notes


async def test_refund_booking_releases_seats(db, trip):
    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Refunder", passenger_phone=_phone(),
        passenger_email=None, seats=2,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()
    await db.refresh(trip)
    assert trip.seats_booked == 2

    refunded = await refund_booking(db, booking, reason="Passenger requested cancellation", refund_method="paystack")
    await db.commit()
    await db.refresh(trip)

    assert refunded.status == BookingStatus.REFUNDED
    assert trip.seats_booked == 0
    assert "Passenger requested cancellation" in refunded.cancellation_reason


async def test_mark_no_show(db, trip):
    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Latecomer", passenger_phone=_phone(),
        passenger_email=None, seats=1,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()

    updated = await mark_no_show(db, booking, reason="Did not arrive at departure gate")
    await db.commit()

    assert updated.status == BookingStatus.NO_SHOW
    assert "Did not arrive at departure gate" in updated.notes


async def test_schedule_conflict_detection(db, route, vehicle):
    dep = now_utc() + timedelta(hours=4)
    arr = dep + timedelta(minutes=90)

    t1 = Trip(
        route_id=route.id,
        service_date=today_lagos(),
        departure_datetime=dep,
        arrival_estimate=arr,
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add(t1)
    await db.commit()

    # Attempting to assign same vehicle to overlapping trip
    with pytest.raises(ConflictError) as exc:
        await assert_no_schedule_conflict(
            db,
            departure_datetime=dep + timedelta(minutes=30),
            arrival_estimate=arr + timedelta(minutes=30),
            vehicle_id=vehicle.id,
        )
    assert "Vehicle is already assigned" in str(exc.value)
