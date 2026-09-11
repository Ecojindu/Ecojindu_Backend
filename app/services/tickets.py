"""QR ticket issuing and validation.

The QR encodes `EJS1.<base64url(payload)>.<base64url(hmac_sha256)>`. Because
the signature is keyed by TICKET_HMAC_SECRET, a ticket cannot be forged or
edited (e.g. bumping the seat count) without the server secret.
"""
from __future__ import annotations

import io
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import log_event
from app.core.security import build_qr_token, parse_qr_token, verify_ticket_signature
from app.models.booking import Booking, Ticket
from app.models.enums import BookingStatus, TripStatus
from app.models.trip import Trip

logger = logging.getLogger("ecojindu.tickets")


def _storage_dir() -> Path:
    path = Path(settings.QR_STORAGE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def render_qr_png(token: str, *, box_size: int = 10, border: int = 2) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(token)
    qr.make(fit=True)
    # Deep forest green on cream keeps the brand identity and still scans well.
    img = qr.make_image(fill_color="#2F5233", back_color="#FFFFFF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_ticket_payload(booking: Booking, trip: Trip) -> dict:
    return {
        "v": 1,
        "ref": booking.booking_ref,
        "bid": str(booking.id),
        "tid": str(trip.id),
        "d": trip.service_date.isoformat(),
        "s": booking.seats,
    }


async def issue_ticket(db: AsyncSession, booking: Booking, trip: Trip) -> Ticket:
    """Create (or refresh) the signed boarding pass for a confirmed booking."""
    payload = build_ticket_payload(booking, trip)
    token = build_qr_token(payload)
    _, signature = parse_qr_token(token)
    encoded_payload = token.split(".")[1]

    png = render_qr_png(token)
    filename = f"{booking.booking_ref}.png"
    path = _storage_dir() / filename
    path.write_bytes(png)

    url = f"{settings.PUBLIC_BASE_URL}/v1/tickets/{booking.booking_ref}/qr.png"

    existing = (
        await db.execute(select(Ticket).where(Ticket.booking_id == booking.id))
    ).scalar_one_or_none()

    if existing:
        existing.qr_payload = encoded_payload
        existing.qr_signature = signature
        existing.qr_token = token
        existing.qr_image_path = str(path)
        existing.qr_image_url = url
        existing.issued_at = datetime.now(timezone.utc)
        ticket = existing
    else:
        ticket = Ticket(
            booking_id=booking.id,
            qr_payload=encoded_payload,
            qr_signature=signature,
            qr_token=token,
            qr_image_path=str(path),
            qr_image_url=url,
            issued_at=datetime.now(timezone.utc),
        )
        db.add(ticket)

    await db.flush()
    log_event(logger, logging.INFO, "ticket issued", booking_ref=booking.booking_ref)
    return ticket


def read_ticket_png(ticket: Ticket) -> bytes:
    """Return the stored PNG, regenerating it if the file is missing (e.g. new pod)."""
    path = Path(ticket.qr_image_path)
    if path.exists():
        return path.read_bytes()
    png = render_qr_png(ticket.qr_token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return png


class ValidationOutcome:
    def __init__(
        self,
        valid: bool,
        status: str,
        message: str,
        booking: Booking | None = None,
        already_checked_in: bool = False,
        checked_in_at: datetime | None = None,
    ):
        self.valid = valid
        self.status = status
        self.message = message
        self.booking = booking
        self.already_checked_in = already_checked_in
        self.checked_in_at = checked_in_at


async def validate_and_check_in(
    db: AsyncSession,
    qr_token: str,
    *,
    checked_in_by: uuid.UUID | None = None,
    expected_trip_id: uuid.UUID | None = None,
) -> ValidationOutcome:
    """Verify a scanned ticket and, if everything checks out, mark it checked in.

    Accepts a bare booking reference too, so terminal staff can type a code when
    a passenger's phone screen is unreadable — the DB lookup is then authoritative.
    """
    token = qr_token.strip()
    booking: Booking | None = None

    if token.startswith("EJS1."):
        try:
            payload, signature = parse_qr_token(token)
        except ValueError as exc:
            return ValidationOutcome(False, "malformed", str(exc))

        if not verify_ticket_signature(payload, signature):
            log_event(logger, logging.WARNING, "ticket signature mismatch", ref=payload.get("ref"))
            return ValidationOutcome(False, "forged", "This ticket's signature is invalid.")

        ref = payload.get("ref")
        booking = (
            await db.execute(select(Booking).where(Booking.booking_ref == ref))
        ).unique().scalar_one_or_none()
    else:
        ref = token.upper()
        if not ref.startswith("EJS-"):
            ref = f"EJS-{ref}"
        booking = (
            await db.execute(select(Booking).where(Booking.booking_ref == ref))
        ).unique().scalar_one_or_none()

    if booking is None:
        return ValidationOutcome(False, "not_found", "No booking matches that ticket.")

    trip = await db.get(Trip, booking.trip_id)
    if trip is None:
        return ValidationOutcome(False, "not_found", "The trip for this ticket no longer exists.")

    if expected_trip_id and trip.id != expected_trip_id:
        return ValidationOutcome(
            False, "wrong_trip", f"This ticket is for the {trip.departure_datetime:%H:%M} departure.", booking
        )

    if booking.status == BookingStatus.CANCELLED:
        return ValidationOutcome(False, "cancelled", "This booking was cancelled.", booking)

    if booking.status == BookingStatus.PENDING_PAYMENT:
        return ValidationOutcome(False, "unpaid", "Payment for this booking is not complete.", booking)

    if trip.status == TripStatus.CANCELLED:
        return ValidationOutcome(False, "trip_cancelled", "This departure was cancelled.", booking)

    today = datetime.now(timezone.utc).date()
    if trip.service_date != today:
        return ValidationOutcome(
            False,
            "wrong_date",
            f"This ticket is valid on {trip.service_date:%a %d %b}, not today.",
            booking,
        )

    ticket = (
        await db.execute(select(Ticket).where(Ticket.booking_id == booking.id))
    ).scalar_one_or_none()

    if ticket and ticket.checked_in_at:
        return ValidationOutcome(
            False,
            "already_checked_in",
            f"Already checked in at {ticket.checked_in_at:%H:%M}.",
            booking,
            already_checked_in=True,
            checked_in_at=ticket.checked_in_at,
        )

    now = datetime.now(timezone.utc)
    if ticket is None:
        ticket = await issue_ticket(db, booking, trip)
    ticket.checked_in_at = now
    ticket.checked_in_by = checked_in_by
    booking.status = BookingStatus.CHECKED_IN
    await db.flush()

    log_event(logger, logging.INFO, "ticket checked in", booking_ref=booking.booking_ref)
    return ValidationOutcome(
        True,
        "checked_in",
        f"Welcome aboard, {booking.passenger_name.split()[0]}.",
        booking,
        checked_in_at=now,
    )


def require_ticket(ticket: Ticket | None) -> Ticket:
    if ticket is None:
        raise NotFoundError("No ticket has been issued for this booking yet.")
    return ticket


def assert_scannable(token: str) -> None:
    if not token or len(token) < 4:
        raise ValidationError("Nothing was scanned.")
