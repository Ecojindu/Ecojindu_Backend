"""Internal job endpoints invoked by Cloud Tasks / the ecojindu-api gateway."""
from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import DbSession, ServiceKey
from app.core.errors import ValidationError
from app.schemas.common import Message
from app.schemas.ticket_reading import SendReminderRequest
from app.services.reminders import send_reminder_for_booking

router = APIRouter(prefix="/internal/jobs", tags=["Internal jobs"])


@router.post(
    "/send-reminder",
    response_model=Message,
    summary="Deliver a scheduled trip reminder (Cloud Tasks / service key)",
)
async def send_reminder(
    body: SendReminderRequest,
    db: DbSession,
    _key: ServiceKey,
) -> Message:
    try:
        booking_id = uuid.UUID(body.booking_id)
    except ValueError as exc:
        raise ValidationError("booking_id must be a UUID.") from exc

    sent = await send_reminder_for_booking(
        db, booking_id=booking_id, reminder_type=body.reminder_type
    )
    await db.commit()
    if sent:
        return Message(message="Reminder sent.")
    return Message(message="Reminder skipped (already sent or booking missing).", ok=True)
