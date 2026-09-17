"""Reminder offset config + scheduling behaviour."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import Settings, settings
from app.core.timeutil import now_utc
from app.models.enums import BookingStatus
from app.services.bookings import confirm_booking, create_booking
from app.services.reminders import schedule_reminders, window_label


def test_reminder_offsets_default_parsed():
    s = Settings(REMINDER_OFFSETS_HOURS="24,3,1")
    assert s.reminder_offsets_hours == [24.0, 3.0, 1.0]


def test_reminder_offsets_custom_string():
    s = Settings(REMINDER_OFFSETS_HOURS="12, 2.5, 0.5")
    assert s.reminder_offsets_hours == [12.0, 2.5, 0.5]


def test_window_label():
    assert window_label(24) == "24h"
    assert window_label(3.0) == "3h"
    assert window_label(2.5) == "2.5h"


def test_seat_hold_default_is_ten():
    assert Settings.model_fields["SEAT_HOLD_MINUTES"].default == 10


@pytest.mark.asyncio
async def test_schedule_reminders_apscheduler_path(db, trip, monkeypatch):
    monkeypatch.setattr(settings, "CLOUD_TASKS_ENABLED", False)
    booking = await create_booking(
        db,
        trip_id=trip.id,
        passenger_name="Ngozi Uche",
        passenger_phone="+2348123456789",
        passenger_email="ngozi@example.com",
        seats=1,
    )
    booking.status = BookingStatus.CONFIRMED
    booking.confirmed_at = now_utc()
    await db.flush()

    planned = await schedule_reminders(db, booking, trip)
    assert len(planned) == len(settings.reminder_offsets_hours)
    types = {p["reminder_type"] for p in planned}
    assert "24h" in types
    assert all(
        p.get("transport") == "apscheduler" or p.get("skipped") == "already_past"
        for p in planned
    )


@pytest.mark.asyncio
async def test_confirm_booking_calls_schedule_reminders(db, trip, monkeypatch):
    monkeypatch.setattr(settings, "CLOUD_TASKS_ENABLED", False)
    booking = await create_booking(
        db,
        trip_id=trip.id,
        passenger_name="Emeka Obi",
        passenger_phone="+2348098765432",
        passenger_email="emeka@example.com",
        seats=1,
    )
    await db.commit()

    with (
        patch(
            "app.services.bookings.notifications.send_booking_confirmation",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.reminders.schedule_reminders",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_sched,
    ):
        await confirm_booking(db, booking, send_notifications=True)
        mock_sched.assert_awaited_once()
