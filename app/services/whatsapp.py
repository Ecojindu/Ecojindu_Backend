"""WhatsApp notification delivery.

Dispatches booking confirmations and boarding pass QR codes directly
to customers via the Ecojindu AI notification service or Twilio WhatsApp.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger("ecojindu.whatsapp")


@dataclass(slots=True)
class WhatsAppResult:
    success: bool
    provider: str
    message_id: str | None = None
    error: str | None = None


def format_whatsapp_recipient(phone: str) -> str:
    p = phone.strip()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    p = "".join(c for c in p if c.isdigit() or c == "+")
    if p.startswith("+"):
        clean = p
    elif p.startswith("0") and len(p) == 11:
        clean = "+234" + p[1:]
    elif p.startswith("234"):
        clean = "+" + p
    else:
        clean = "+234" + p
    return f"whatsapp:{clean}"


async def send_whatsapp(
    to: str,
    message: str,
    *,
    media_url: str | None = None,
    booking_ref: str | None = None,
    passenger_name: str | None = None,
    origin: str | None = None,
    destination: str | None = None,
    departure_time: str | None = None,
    seats: int = 1,
    seat_numbers: list[str] | None = None,
) -> WhatsAppResult:
    """Send a WhatsApp message with optional QR image ticket attachment.

    Attempts dispatch via Ecojindu AI notification service first (to maintain
    conversational chat history in the operations admin portal), falling back
    to direct Twilio API if AI service is unavailable, and logs in console mode
    for development/tests.
    """
    if not settings.WHATSAPP_ENABLED:
        return WhatsAppResult(False, "disabled", error="WHATSAPP_ENABLED=false")

    wa_phone = format_whatsapp_recipient(to)

    # 1. Try Ecojindu AI notification service
    if settings.AI_SERVICE_URL:
        try:
            payload = {
                "phone": wa_phone,
                "booking_ref": booking_ref or "EJS-BOOKING",
                "passenger_name": passenger_name or "Valued Passenger",
                "origin": origin or "Terminal",
                "destination": destination or "Destination",
                "departure_time": departure_time or "",
                "seats": seats,
                "seat_numbers": seat_numbers or [],
                "qr_image_url": media_url or "",
                "message": message,
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{settings.AI_SERVICE_URL.rstrip('/')}/notifications/send-ticket",
                    json=payload,
                )
            if resp.status_code == 200:
                data = resp.json()
                return WhatsAppResult(True, "ecojindu-ai", message_id=data.get("sid"))
            log_event(logger, logging.WARNING, "AI service WhatsApp dispatch failed", status=resp.status_code)
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.WARNING, "AI service WhatsApp error", error=str(exc))

    # 2. Direct Twilio fallback if Twilio credentials are configured
    sid, token = settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN
    if sid and token:
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        data = {
            "To": wa_phone,
            "From": settings.TWILIO_WHATSAPP_FROM,
            "Body": message,
        }
        if media_url:
            data["MediaUrl"] = media_url

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                    data=data,
                    headers={"Authorization": f"Basic {auth}"},
                )
            data_resp = resp.json()
            if resp.status_code < 300:
                return WhatsAppResult(True, "twilio", message_id=data_resp.get("sid"))
            return WhatsAppResult(False, "twilio", error=str(data_resp)[:400])
        except Exception as exc:  # noqa: BLE001
            return WhatsAppResult(False, "twilio", error=str(exc)[:400])

    # 3. Development / Test console sink
    log_event(logger, logging.INFO, "WhatsApp (console)", to=wa_phone, media_url=media_url, body=message.replace("\n", " | "))
    return WhatsAppResult(True, "console", message_id="console-wa-" + to[-4:])
