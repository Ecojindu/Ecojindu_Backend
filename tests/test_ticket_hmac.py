"""QR tickets must be unforgeable and single-use on the day of travel."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from app.core.security import (
    _b64u,
    build_qr_token,
    parse_qr_token,
    sign_ticket_payload,
    verify_ticket_signature,
)
from app.core.timeutil import combine_lagos, now_utc, today_lagos
from app.models.enums import BookingStatus, TripStatus
from app.services.bookings import confirm_booking, create_booking
from app.services.tickets import build_ticket_payload, issue_ticket, validate_and_check_in


def _phone() -> str:
    return f"+23481{uuid.uuid4().int % 100_000_000:08d}"


def test_signature_round_trips():
    payload = {"v": 1, "ref": "EJS-8K3F2", "bid": "abc", "tid": "def", "d": "2026-08-13", "s": 2}
    token = build_qr_token(payload)
    decoded, signature = parse_qr_token(token)

    assert decoded == payload
    assert verify_ticket_signature(decoded, signature)


def test_tampering_with_the_payload_breaks_the_signature():
    payload = {"v": 1, "ref": "EJS-8K3F2", "bid": "abc", "tid": "def", "d": "2026-08-13", "s": 1}
    _, signature = parse_qr_token(build_qr_token(payload))

    # A passenger bumping their seat count from 1 to 4.
    forged = {**payload, "s": 4}
    assert not verify_ticket_signature(forged, signature)


def test_signature_is_key_dependent(monkeypatch):
    payload = {"v": 1, "ref": "EJS-8K3F2", "s": 1}
    original = sign_ticket_payload(payload)

    from app.core import security

    monkeypatch.setattr(security.settings, "TICKET_HMAC_SECRET", "a-different-secret")
    assert sign_ticket_payload(payload) != original


def test_malformed_tokens_are_rejected():
    for bad in ["", "nonsense", "EJS1.only-two-parts", "EJS2.aaa.bbb", "EJS1.!!!.bbb"]:
        try:
            parse_qr_token(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse")


async def test_issue_and_validate_ticket_checks_passenger_in(db, route, vehicle):
    from app.models.trip import Trip

    # A departure later today so the date check passes.
    departure = now_utc() + timedelta(hours=3)
    trip = Trip(
        route_id=route.id,
        service_date=today_lagos(),
        departure_datetime=departure,
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add(trip)
    await db.commit()

    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Ifeanyi Duru",
        passenger_phone=_phone(), passenger_email="ifeanyi@example.com", seats=1,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()
    await db.refresh(booking, ["ticket"])

    token = booking.ticket.qr_token

    first = await validate_and_check_in(db, token)
    await db.commit()
    assert first.valid is True
    assert first.status == "checked_in"

    # A second scan of the same pass must not silently succeed.
    second = await validate_and_check_in(db, token)
    await db.commit()
    assert second.valid is False
    assert second.status == "already_checked_in"
    assert second.already_checked_in is True

    await db.refresh(booking)
    assert booking.status == BookingStatus.CHECKED_IN


async def test_forged_ticket_is_refused(db, route, vehicle):
    from app.models.trip import Trip

    trip = Trip(
        route_id=route.id,
        service_date=today_lagos(),
        departure_datetime=now_utc() + timedelta(hours=3),
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add(trip)
    await db.commit()

    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Forger", passenger_phone=_phone(),
        passenger_email=None, seats=1,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()

    payload = build_ticket_payload(booking, trip)
    forged_payload = {**payload, "s": 10}
    _, real_signature = parse_qr_token(build_qr_token(payload))
    forged_token = (
        "EJS1."
        + _b64u(json.dumps(forged_payload, sort_keys=True, separators=(",", ":")).encode())
        + "."
        + real_signature
    )

    outcome = await validate_and_check_in(db, forged_token)
    assert outcome.valid is False
    assert outcome.status == "forged"


async def test_ticket_for_another_day_is_refused(db, trip):
    """`trip` departs tomorrow — scanning it today must fail."""
    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Early Bird", passenger_phone=_phone(),
        passenger_email=None, seats=1,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()
    await db.refresh(booking, ["ticket"])

    outcome = await validate_and_check_in(db, booking.ticket.qr_token)
    assert outcome.valid is False
    assert outcome.status == "wrong_date"


async def test_unpaid_booking_cannot_check_in(db, route, vehicle):
    from app.models.trip import Trip

    trip = Trip(
        route_id=route.id,
        service_date=today_lagos(),
        departure_datetime=now_utc() + timedelta(hours=3),
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add(trip)
    await db.commit()

    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Unpaid", passenger_phone=_phone(),
        passenger_email=None, seats=1,
    )
    ticket = await issue_ticket(db, booking, trip)
    await db.commit()

    outcome = await validate_and_check_in(db, ticket.qr_token)
    assert outcome.valid is False
    assert outcome.status == "unpaid"


async def test_bare_reference_can_be_typed_in_manually(db, route, vehicle):
    from app.models.trip import Trip

    trip = Trip(
        route_id=route.id,
        service_date=today_lagos(),
        departure_datetime=now_utc() + timedelta(hours=3),
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add(trip)
    await db.commit()

    booking = await create_booking(
        db, trip_id=trip.id, passenger_name="Cracked Screen", passenger_phone=_phone(),
        passenger_email=None, seats=1,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()

    outcome = await validate_and_check_in(db, booking.booking_ref)
    await db.commit()
    assert outcome.valid is True
    assert outcome.booking.booking_ref == booking.booking_ref
