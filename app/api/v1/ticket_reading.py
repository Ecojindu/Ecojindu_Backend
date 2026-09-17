"""Upload & Go — ticket reading + shuttle match endpoints."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import DbSession
from app.core.config import settings
from app.core.errors import ValidationError
from app.core.ratelimit import RateLimiter
from app.core.timeutil import LAGOS
from app.schemas.ticket_reading import (
    ShuttleSuggestionOut,
    TicketExtractionOut,
    TicketMatchRequest,
    TicketReadingResponse,
)
from app.services import shuttle_match, ticket_reading

router = APIRouter(prefix="/ticket-reading", tags=["Ticket reading"])

_rate_limit = RateLimiter(
    "ticket_reading",
    limit=settings.TICKET_READ_RATE_LIMIT_PER_MINUTE,
    window_seconds=60,
)

PRIVACY_NOTE = "Uploaded ticket was deleted after reading. We keep only the fields shown."


def _parse_departure(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("departure_datetime must be a valid ISO 8601 datetime.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LAGOS)
    return dt


@router.post(
    "",
    response_model=TicketReadingResponse,
    summary="Read a boarding pass and suggest a shuttle",
    dependencies=[Depends(_rate_limit)],
)
async def read_ticket(
    db: DbSession,
    file: UploadFile | None = File(None),
    pnr: str | None = Form(None),
    pickup_city: str = Form("Umuahia"),
) -> TicketReadingResponse:
    file_bytes: bytes | None = None
    content_type: str | None = None
    if file is not None and file.filename:
        content_type = file.content_type
        file_bytes = await file.read()
        # Cap read against configured max (UploadFile may stream more).
        if len(file_bytes) > settings.TICKET_READ_MAX_BYTES:
            file_bytes = None
            raise ValidationError(
                f"That file is too large. Maximum size is "
                f"{settings.TICKET_READ_MAX_BYTES // (1024 * 1024)} MB.",
                code="file_too_large",
            )

    extraction = await ticket_reading.extract_ticket(
        file_bytes=file_bytes,
        content_type=content_type,
        pnr=pnr,
    )
    # Explicit discard after extract_ticket returns (defence in depth).
    file_bytes = None

    dep = _parse_departure(extraction.departure_datetime)
    if dep is None:
        suggestion = ShuttleSuggestionOut(
            trip=None,
            fits=False,
            check_in_by="",
            message=(
                "We could not determine the flight departure time. "
                "Edit the time and re-match, or enter details manually."
            ),
            alternatives=[],
        )
    else:
        raw = await shuttle_match.match_shuttle(
            db, flight_departure=dep, pickup_city=pickup_city
        )
        suggestion = ShuttleSuggestionOut(
            trip=raw.trip,
            fits=raw.fits,
            check_in_by=raw.check_in_by,
            message=raw.message,
            alternatives=raw.alternatives,
        )

    return TicketReadingResponse(
        extraction=TicketExtractionOut(**extraction.to_dict()),
        suggestion=suggestion,
        privacy_note=PRIVACY_NOTE,
    )


@router.post(
    "/match",
    response_model=ShuttleSuggestionOut,
    summary="Re-match a shuttle after editing flight time or pickup city",
    dependencies=[Depends(_rate_limit)],
)
async def rematch_shuttle(db: DbSession, body: TicketMatchRequest) -> ShuttleSuggestionOut:
    raw = await shuttle_match.match_shuttle(
        db,
        flight_departure=body.departure_datetime,
        pickup_city=body.pickup_city,
    )
    return ShuttleSuggestionOut(
        trip=raw.trip,
        fits=raw.fits,
        check_in_by=raw.check_in_by,
        message=raw.message,
        alternatives=raw.alternatives,
    )
