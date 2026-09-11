"""SMS delivery behind one provider interface.

Termii is primary (Nigeria-focused, good DND handling); Twilio is a drop-in
fallback; `console` prints to the log so the whole system runs locally with
no credentials at all.
"""
from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger("ecojindu.sms")


@dataclass(slots=True)
class SMSResult:
    success: bool
    provider: str
    message_id: str | None = None
    error: str | None = None


class SMSProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def send(self, to: str, message: str) -> SMSResult: ...


class ConsoleSMSProvider(SMSProvider):
    """Development sink — logs the exact text that would have been sent."""

    name = "console"

    async def send(self, to: str, message: str) -> SMSResult:
        log_event(logger, logging.INFO, "SMS (console)", to=to, body=message.replace("\n", " | "))
        return SMSResult(True, self.name, message_id="console-" + to[-4:])


class TermiiSMSProvider(SMSProvider):
    name = "termii"

    async def send(self, to: str, message: str) -> SMSResult:
        if not settings.TERMII_API_KEY:
            return SMSResult(False, self.name, error="TERMII_API_KEY is not configured")
        payload = {
            "to": to.lstrip("+"),
            "from": settings.TERMII_SENDER_ID,
            "sms": message,
            "type": "plain",
            "channel": "generic",
            "api_key": settings.TERMII_API_KEY,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{settings.TERMII_BASE_URL}/api/sms/send", json=payload
                )
            data = resp.json()
            if resp.status_code < 300 and str(data.get("code", "")).lower() in {"ok", "200", ""}:
                return SMSResult(True, self.name, message_id=data.get("message_id"))
            return SMSResult(False, self.name, error=str(data)[:400])
        except Exception as exc:  # noqa: BLE001 - never let a provider outage break a booking
            return SMSResult(False, self.name, error=str(exc)[:400])


class TwilioSMSProvider(SMSProvider):
    name = "twilio"

    async def send(self, to: str, message: str) -> SMSResult:
        sid, token = settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN
        if not sid or not token:
            return SMSResult(False, self.name, error="Twilio credentials are not configured")
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                    data={"To": to, "From": settings.TWILIO_SMS_FROM, "Body": message},
                    headers={"Authorization": f"Basic {auth}"},
                )
            data = resp.json()
            if resp.status_code < 300:
                return SMSResult(True, self.name, message_id=data.get("sid"))
            return SMSResult(False, self.name, error=str(data)[:400])
        except Exception as exc:  # noqa: BLE001
            return SMSResult(False, self.name, error=str(exc)[:400])


_PROVIDERS: dict[str, type[SMSProvider]] = {
    "console": ConsoleSMSProvider,
    "termii": TermiiSMSProvider,
    "twilio": TwilioSMSProvider,
}


def get_sms_provider(name: str | None = None) -> SMSProvider:
    key = (name or settings.SMS_PROVIDER).lower()
    return _PROVIDERS.get(key, ConsoleSMSProvider)()


async def send_sms(to: str, message: str) -> SMSResult:
    """Send via the configured provider, falling back to Twilio if Termii fails."""
    if not settings.SMS_ENABLED:
        return SMSResult(False, "disabled", error="SMS_ENABLED=false")

    primary = get_sms_provider()
    result = await primary.send(to, message)
    if result.success:
        return result

    if primary.name == "termii" and settings.TWILIO_ACCOUNT_SID:
        log_event(logger, logging.WARNING, "Termii failed, falling back to Twilio", error=result.error)
        return await TwilioSMSProvider().send(to, message)

    log_event(logger, logging.ERROR, "SMS send failed", provider=result.provider, error=result.error)
    return result
