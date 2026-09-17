"""Upload & Go — extract flight details from a boarding pass image or PDF.

Uploaded bytes are held only in memory for the Anthropic call, then discarded.
Nothing is written to disk.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.core.config import settings
from app.core.errors import AppError, ValidationError
from app.core.logging import log_event

logger = logging.getLogger("ecojindu.ticket_reading")

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "application/pdf",
    }
)

_EXTRACTION_PROMPT = """Extract flight booking details from this boarding pass / e-ticket.
Return ONLY a JSON object with these keys (use null when unknown):
{
  "passenger_names": ["string"],
  "airline": "string or null",
  "flight_number": "string or null",
  "departure_airport": "IATA code or null",
  "departure_datetime": "ISO 8601 datetime or null",
  "arrival_airport": "IATA code or null",
  "arrival_datetime": "ISO 8601 datetime or null",
  "pnr": "string or null"
}
Prefer Africa/Lagos local time when the ticket does not specify a timezone.
If a PNR hint is provided below, use it to disambiguate when multiple bookings appear.
"""


@dataclass(slots=True)
class TicketExtraction:
    passenger_names: list[str] = field(default_factory=list)
    airline: str | None = None
    flight_number: str | None = None
    departure_airport: str | None = None
    departure_datetime: str | None = None
    arrival_airport: str | None = None
    arrival_datetime: str | None = None
    pnr: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def validate_ticket_upload(content_type: str | None, size: int) -> str:
    """Normalise and validate content-type + size. Returns a canonical mime."""
    if size <= 0:
        raise ValidationError("Please upload a ticket image or PDF.")
    if size > settings.TICKET_READ_MAX_BYTES:
        raise ValidationError(
            f"That file is too large. Maximum size is {settings.TICKET_READ_MAX_BYTES // (1024 * 1024)} MB.",
            code="file_too_large",
        )
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            "Please upload a JPEG, PNG, WebP image or a PDF of your boarding pass.",
            code="unsupported_media_type",
            details={"allowed": sorted(ALLOWED_CONTENT_TYPES - {"image/jpg"})},
        )
    return mime


def _dev_mock_extraction(pnr: str | None) -> TicketExtraction:
    """Deterministic mock used when ENVIRONMENT=development and no API key."""
    return TicketExtraction(
        passenger_names=["Ada Okoro"],
        airline="Air Peace",
        flight_number="P47123",
        departure_airport="QOW",
        departure_datetime="2026-09-17T14:00:00+01:00",
        arrival_airport="LOS",
        arrival_datetime="2026-09-17T15:15:00+01:00",
        pnr=pnr or "ABC123",
    )


def _parse_extraction_json(raw: str, pnr_hint: str | None) -> TicketExtraction:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(
            "We could not read that ticket clearly. Please enter your flight details manually.",
            code="ticket_parse_failed",
        ) from exc

    names = data.get("passenger_names") or []
    if isinstance(names, str):
        names = [names]
    names = [str(n).strip() for n in names if str(n).strip()]

    dep = data.get("departure_datetime")
    if dep is not None:
        dep = str(dep).strip() or None
        if dep:
            try:
                # Normalise to ISO; accept naive as Lagos-ish +01:00 if no tz.
                parsed = datetime.fromisoformat(dep.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                dep = parsed.isoformat()
            except ValueError:
                pass

    return TicketExtraction(
        passenger_names=names,
        airline=(str(data["airline"]).strip() if data.get("airline") else None) or None,
        flight_number=(str(data["flight_number"]).strip() if data.get("flight_number") else None) or None,
        departure_airport=(
            str(data["departure_airport"]).strip().upper() if data.get("departure_airport") else None
        )
        or None,
        departure_datetime=dep,
        arrival_airport=(
            str(data["arrival_airport"]).strip().upper() if data.get("arrival_airport") else None
        )
        or None,
        arrival_datetime=(str(data["arrival_datetime"]).strip() if data.get("arrival_datetime") else None)
        or None,
        pnr=(str(data["pnr"]).strip().upper() if data.get("pnr") else None) or (pnr_hint or None),
    )


async def extract_ticket(
    *,
    file_bytes: bytes | None = None,
    content_type: str | None = None,
    pnr: str | None = None,
) -> TicketExtraction:
    """Extract flight fields from uploaded ticket bytes and/or a PNR hint.

    Always clears the local reference to `file_bytes` after the API call so the
    payload is eligible for GC and is never persisted.
    """
    pnr_clean = (pnr or "").strip().upper() or None

    if file_bytes is None and not pnr_clean:
        raise ValidationError("Upload a ticket image/PDF or enter a PNR.")

    mime: str | None = None
    if file_bytes is not None:
        mime = validate_ticket_upload(content_type, len(file_bytes))

    if not settings.ANTHROPIC_API_KEY:
        if settings.ENVIRONMENT == "development":
            log_event(logger, logging.INFO, "ticket read using development mock")
            file_bytes = None  # discard
            return _dev_mock_extraction(pnr_clean)
        # Drop bytes before raising so nothing lingers on the stack frame.
        file_bytes = None
        raise AppError(
            "Automatic ticket reading is not configured. Please enter your flight details manually.",
            code="ticket_reading_unavailable",
        )

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        file_bytes = None
        raise AppError(
            "Ticket reading is temporarily unavailable. Please enter your flight details manually.",
            code="ticket_reading_unavailable",
        ) from exc

    content_blocks: list[dict] = []
    if file_bytes is not None and mime is not None:
        b64 = base64.standard_b64encode(file_bytes).decode("ascii")
        # Discard raw upload immediately after encoding.
        file_bytes = None
        if mime == "application/pdf":
            content_blocks.append(
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                }
            )
        else:
            content_blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                }
            )
        del b64

    prompt = _EXTRACTION_PROMPT
    if pnr_clean:
        prompt += f"\nPNR hint: {pnr_clean}\n"
    content_blocks.append({"type": "text", "text": prompt})

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=45.0)
    try:
        message = await client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": content_blocks}],
        )
    except Exception as exc:  # noqa: BLE001
        log_event(logger, logging.ERROR, "anthropic ticket read failed", error=str(exc))
        raise AppError(
            "We could not read that ticket right now. Please enter your flight details manually.",
            code="ticket_reading_failed",
        ) from exc
    finally:
        # Ensure no lingering upload reference.
        file_bytes = None
        content_blocks = []

    raw_parts = [
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ]
    if not raw_parts:
        raise AppError(
            "We could not read that ticket clearly. Please enter your flight details manually.",
            code="ticket_parse_failed",
        )

    extraction = _parse_extraction_json("\n".join(raw_parts), pnr_clean)
    log_event(
        logger,
        logging.INFO,
        "ticket extracted",
        airline=extraction.airline,
        flight=extraction.flight_number,
        dep=extraction.departure_airport,
    )
    return extraction
