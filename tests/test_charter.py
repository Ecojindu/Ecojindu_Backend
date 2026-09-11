"""Charter hire: quoting, payment, and the trip it materialises."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError, ValidationError
from app.core.timeutil import today_lagos
from app.models.charter import CharterRequest
from app.models.enums import CharterStatus, PaymentStatus, TripStatus
from app.models.payment import Payment
from app.models.trip import Trip
from app.services import charter as charter_service
from app.services.payments import process_paystack_event


async def _request(db, route, *, days_ahead=3, passengers=10):
    return await charter_service.create_request(
        db,
        contact_name="Chinwe Eze",
        contact_phone="+2348033322211",
        contact_email="chinwe@example.com",
        organisation="Abia State Ministry of Transport",
        route_id=route.id,
        origin_text="Umuahia Government House",
        destination_text="Sam Mbakwe Airport",
        service_date=today_lagos() + timedelta(days=days_ahead),
        preferred_time=None,
        passengers=passengers,
        return_trip=True,
        notes=None,
    )


async def test_request_creates_a_reference_and_starts_unquoted(db, route):
    charter = await _request(db, route)
    await db.commit()

    assert charter.reference.startswith("EJC-")
    assert charter.status == CharterStatus.REQUESTED
    assert charter.quoted_amount_kobo is None
    # No 0/O/1/I — the reference has to survive being read down a phone line.
    assert not set(charter.reference.removeprefix("EJC-")) & set("01OI")


async def test_charter_needs_lead_time(db, route):
    with pytest.raises(ValidationError) as exc:
        await charter_service.create_request(
            db,
            contact_name="Too Soon",
            contact_phone="+2348033322299",
            contact_email=None,
            organisation=None,
            route_id=route.id,
            origin_text="Umuahia Terminal",
            destination_text="Sam Mbakwe Airport",
            service_date=today_lagos(),
            preferred_time=None,
            passengers=2,
            return_trip=False,
            notes=None,
        )
    assert "12 hours" in exc.value.message


async def test_past_dates_are_rejected(db, route):
    with pytest.raises(ValidationError):
        await charter_service.create_request(
            db,
            contact_name="Time Traveller",
            contact_phone="+2348033322298",
            contact_email=None,
            organisation=None,
            route_id=route.id,
            origin_text="Umuahia Terminal",
            destination_text="Sam Mbakwe Airport",
            service_date=today_lagos() - timedelta(days=1),
            preferred_time=None,
            passengers=2,
            return_trip=False,
            notes=None,
        )


async def test_quote_then_payment_confirms(db, route, vehicle):
    charter = await _request(db, route)
    await charter_service.quote(
        db, charter, amount_kobo=21_000_000, quote_notes=None, vehicle_id=None,
        quoted_by=None, notify=False,
    )
    await db.commit()
    assert charter.status == CharterStatus.QUOTED

    payment = Payment(
        charter_request_id=charter.id,
        paystack_reference=f"EJSCHT-{charter.reference}",
        amount_kobo=21_000_000,
        status=PaymentStatus.PENDING,
        customer_email="chinwe@example.com",
    )
    db.add(payment)
    await db.commit()

    await process_paystack_event(
        db,
        {
            "event": "charge.success",
            "data": {"reference": payment.paystack_reference, "amount": 21_000_000, "channel": "card"},
        },
    )
    await db.commit()

    refreshed = (
        await db.execute(select(CharterRequest).where(CharterRequest.id == charter.id))
    ).unique().scalar_one()
    assert refreshed.status == CharterStatus.CONFIRMED
    assert refreshed.confirmed_at is not None


async def test_cannot_assign_before_payment(db, route, vehicle):
    charter = await _request(db, route)
    await db.commit()

    with pytest.raises(ConflictError):
        await charter_service.assign(db, charter, vehicle_id=vehicle.id, driver_id=None)


async def test_assign_materialises_a_fully_booked_trip(db, route, vehicle):
    """The whole vehicle is hired, so the trip must never show public availability."""
    charter = await _request(db, route, passengers=10)
    await charter_service.quote(
        db, charter, amount_kobo=21_000_000, quote_notes=None, vehicle_id=None,
        quoted_by=None, notify=False,
    )
    await charter_service.mark_confirmed(db, charter)
    await charter_service.assign(db, charter, vehicle_id=vehicle.id, driver_id=None)
    await db.commit()

    assert charter.status == CharterStatus.ASSIGNED
    assert charter.trip_id is not None

    trip = await db.get(Trip, charter.trip_id)
    assert trip.seats_booked == trip.seats_total == vehicle.seat_capacity
    assert trip.seats_available == 0
    assert trip.status == TripStatus.SCHEDULED


async def test_vehicle_must_be_big_enough(db, route, vehicle):
    charter = await _request(db, route, passengers=vehicle.seat_capacity + 5)
    await charter_service.quote(
        db, charter, amount_kobo=30_000_000, quote_notes=None, vehicle_id=None,
        quoted_by=None, notify=False,
    )
    await charter_service.mark_confirmed(db, charter)
    await db.commit()

    with pytest.raises(ConflictError) as exc:
        await charter_service.assign(db, charter, vehicle_id=vehicle.id, driver_id=None)
    assert "seats" in exc.value.message


async def test_paid_charter_cannot_be_requoted(db, route):
    charter = await _request(db, route)
    await charter_service.quote(
        db, charter, amount_kobo=21_000_000, quote_notes=None, vehicle_id=None,
        quoted_by=None, notify=False,
    )
    await charter_service.mark_confirmed(db, charter)
    await db.commit()

    with pytest.raises(ConflictError):
        await charter_service.quote(
            db, charter, amount_kobo=99_000_000, quote_notes=None, vehicle_id=None,
            quoted_by=None, notify=False,
        )


async def test_cancelling_also_cancels_the_trip(db, route, vehicle):
    charter = await _request(db, route)
    await charter_service.quote(
        db, charter, amount_kobo=21_000_000, quote_notes=None, vehicle_id=None,
        quoted_by=None, notify=False,
    )
    await charter_service.mark_confirmed(db, charter)
    await charter_service.assign(db, charter, vehicle_id=vehicle.id, driver_id=None)
    await db.commit()

    trip_id = charter.trip_id
    await charter_service.cancel(db, charter, reason="Vehicle unavailable", notify=False)
    await db.commit()

    assert charter.status == CharterStatus.CANCELLED
    trip = await db.get(Trip, trip_id, populate_existing=True)
    assert trip.status == TripStatus.CANCELLED


async def test_lookup_requires_the_matching_phone(db, route):
    from app.core.errors import NotFoundError

    charter = await _request(db, route)
    await db.commit()

    found = await charter_service.lookup(db, charter.reference, "+2348033322211")
    assert found.id == charter.id

    # A reference alone must not confirm a charter exists against another number.
    with pytest.raises(NotFoundError):
        await charter_service.lookup(db, charter.reference, "+2348099999999")
