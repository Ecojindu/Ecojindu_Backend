"""Upload & Go — ticket validation, Anthropic parsing, shuttle matching."""
from __future__ import annotations

from datetime import datetime, timedelta, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.errors import AppError, ValidationError
from app.core.timeutil import combine_lagos, today_lagos
from app.models.enums import TripStatus
from app.models.trip import Trip
from app.services import shuttle_match, ticket_reading


def test_reject_oversize_upload():
    big = b"x" * (settings.TICKET_READ_MAX_BYTES + 1)
    with pytest.raises(ValidationError) as exc:
        ticket_reading.validate_ticket_upload("image/jpeg", len(big))
    assert exc.value.code == "file_too_large"


def test_reject_bad_mime():
    with pytest.raises(ValidationError) as exc:
        ticket_reading.validate_ticket_upload("application/zip", 100)
    assert exc.value.code == "unsupported_media_type"


def test_accept_allowed_mimes():
    assert ticket_reading.validate_ticket_upload("image/jpg", 100) == "image/jpeg"
    assert ticket_reading.validate_ticket_upload("image/png", 100) == "image/png"
    assert ticket_reading.validate_ticket_upload("application/pdf", 100) == "application/pdf"


@pytest.mark.asyncio
async def test_dev_mock_when_no_api_key(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    result = await ticket_reading.extract_ticket(
        file_bytes=b"fake-jpeg",
        content_type="image/jpeg",
        pnr="XYZ999",
    )
    assert result.passenger_names == ["Ada Okoro"]
    assert result.flight_number == "P47123"
    assert result.departure_airport == "QOW"
    assert result.pnr == "XYZ999"


@pytest.mark.asyncio
async def test_no_key_non_dev_asks_for_manual(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(AppError) as exc:
        await ticket_reading.extract_ticket(
            file_bytes=b"fake",
            content_type="image/jpeg",
        )
    assert exc.value.code == "ticket_reading_unavailable"


@pytest.mark.asyncio
async def test_mock_anthropic_response_parsed(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    payload = (
        '{"passenger_names":["Chidi Eze"],"airline":"Ibom Air",'
        '"flight_number":"QI550","departure_airport":"QOW",'
        '"departure_datetime":"2026-09-17T14:00:00+01:00",'
        '"arrival_airport":"ABV","arrival_datetime":null,"pnr":"IB9K2"}'
    )
    fake_message = SimpleNamespace(content=[SimpleNamespace(type="text", text=payload)])
    instance = MagicMock()
    instance.messages.create = AsyncMock(return_value=fake_message)
    mock_cls = MagicMock(return_value=instance)
    mock_module = MagicMock(AsyncAnthropic=mock_cls)

    with patch.dict("sys.modules", {"anthropic": mock_module}):
        result = await ticket_reading.extract_ticket(
            file_bytes=b"\xff\xd8\xffjpeg",
            content_type="image/jpeg",
            pnr=None,
        )

    assert result.passenger_names == ["Chidi Eze"]
    assert result.airline == "Ibom Air"
    assert result.flight_number == "QI550"
    assert result.departure_airport == "QOW"
    assert result.departure_datetime is not None
    assert "14:00" in result.departure_datetime
    assert result.pnr == "IB9K2"
    mock_cls.assert_called_once()
    instance.messages.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_shuttle_match_prefers_morning_before_check_in(db, route, vehicle):
    """Flight at 14:00 with 2.5h buffer → check-in by 11:30 → morning trip preferred."""
    day = today_lagos() + timedelta(days=1)

    morning_dep = combine_lagos(day, time(8, 0))
    morning = Trip(
        route_id=route.id,
        service_date=day,
        departure_datetime=morning_dep,
        arrival_estimate=morning_dep + timedelta(minutes=90),  # 09:30
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    late_morning_dep = combine_lagos(day, time(9, 30))
    late_morning = Trip(
        route_id=route.id,
        service_date=day,
        departure_datetime=late_morning_dep,
        arrival_estimate=late_morning_dep + timedelta(minutes=90),  # 11:00 — still before 11:30
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    noon_dep = combine_lagos(day, time(10, 30))
    noon = Trip(
        route_id=route.id,
        service_date=day,
        departure_datetime=noon_dep,
        arrival_estimate=noon_dep + timedelta(minutes=90),  # 12:00 — after 11:30
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add_all([morning, late_morning, noon])
    await db.commit()

    # Reload route onto trips via search; ensure fixture route is airport-bound.
    assert "Airport" in route.destination or "Mbakwe" in route.destination

    flight = combine_lagos(day, time(14, 0))
    monkey_buffer = settings.CHECK_IN_BUFFER_HOURS
    assert monkey_buffer == 2.5

    suggestion = await shuttle_match.match_shuttle(
        db, flight_departure=flight, pickup_city="Umuahia"
    )

    assert suggestion.fits is True
    assert suggestion.trip is not None
    # Latest trip that arrives before 11:30 is the 09:30 departure (arrives 11:00).
    assert suggestion.trip.id == late_morning.id
    check_in = datetime.fromisoformat(suggestion.check_in_by)
    assert check_in.hour == 11 and check_in.minute == 30


@pytest.mark.asyncio
async def test_shuttle_match_no_fit_returns_nearest(db, route, vehicle):
    day = today_lagos() + timedelta(days=2)
    only_dep = combine_lagos(day, time(12, 0))
    only = Trip(
        route_id=route.id,
        service_date=day,
        departure_datetime=only_dep,
        arrival_estimate=only_dep + timedelta(minutes=90),  # 13:30, after 11:30 check-in
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=14,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add(only)
    await db.commit()

    flight = combine_lagos(day, time(14, 0))
    suggestion = await shuttle_match.match_shuttle(
        db, flight_departure=flight, pickup_city="Umuahia"
    )
    assert suggestion.fits is False
    assert suggestion.trip is not None
    assert suggestion.trip.id == only.id
    assert "tight" in suggestion.message.lower() or "check-in" in suggestion.message.lower()


@pytest.mark.asyncio
async def test_flight_status_unknown_without_key(monkeypatch):
    monkeypatch.setattr(settings, "AVIATIONSTACK_API_KEY", "")
    from app.services import flights as flight_service

    result = await flight_service.get_flight_status(flight_number="P47123")
    assert result["status"] == "unknown"
    assert "AVIATIONSTACK" in result["message"] or "unavailable" in result["message"].lower()
