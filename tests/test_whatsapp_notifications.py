import pytest
from sqlalchemy import select
from app.models.booking import Booking, Ticket
from app.models.enums import NotificationChannel, NotificationStatus, NotificationType
from app.models.notification import Notification
from app.services.whatsapp import format_whatsapp_recipient, send_whatsapp
from app.services.notifications import send_booking_confirmation
from app.services.bookings import create_booking, confirm_booking

def test_format_whatsapp_recipient():
    assert format_whatsapp_recipient("08012345678") == "whatsapp:+2348012345678"
    assert format_whatsapp_recipient("+2348012345678") == "whatsapp:+2348012345678"
    assert format_whatsapp_recipient("2348012345678") == "whatsapp:+2348012345678"
    assert format_whatsapp_recipient("whatsapp:+2348012345678") == "whatsapp:+2348012345678"


@pytest.mark.asyncio
async def test_send_booking_confirmation_records_whatsapp(db, trip, route):
    booking = await create_booking(
        db,
        trip_id=trip.id,
        passenger_name="Chukwuma Soludo",
        passenger_phone="+2348123456789",
        passenger_email="soludo@example.com",
        seats=1,
    )
    await confirm_booking(db, booking, send_notifications=False)
    await db.commit()
    await db.refresh(booking, ["ticket"])

    # Now call send_booking_confirmation
    await send_booking_confirmation(db, booking, trip, route, booking.ticket)
    await db.commit()

    # Verify that a Notification record for WHATSAPP exists
    stmt = (
        select(Notification)
        .where(
            Notification.booking_id == booking.id,
            Notification.channel == NotificationChannel.WHATSAPP,
        )
    )
    res = (await db.execute(stmt)).scalars().first()
    assert res is not None
    assert res.type == NotificationType.CONFIRMATION
    assert res.recipient == "+2348123456789"
    assert res.status == NotificationStatus.SENT
    assert res.payload is not None
    assert "media_url" in res.payload
    assert "qr.png" in res.payload["media_url"]
