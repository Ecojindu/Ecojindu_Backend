"""QR ticket delivery and scanner validation."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.deps import DbSession, OptionalUser, StaffUser
from app.core.errors import ForbiddenError, NotFoundError
from app.core.security import constant_time_equals, parse_qr_token, verify_ticket_signature
from app.core.timeutil import fmt_datetime
from app.models.enums import UserRole
from app.models.route import Route
from app.models.trip import Trip
from app.schemas.booking import ValidateTicketRequest, ValidateTicketResponse
from app.services import audit as audit_service
from app.services.bookings import get_booking_by_ref, get_ticket
from app.services.tickets import read_ticket_png, validate_and_check_in

router = APIRouter(prefix="/tickets", tags=["Tickets"])


@router.get(
    "/{booking_ref}/qr.png",
    response_class=Response,
    summary="The boarding-pass QR image",
    responses={200: {"content": {"image/png": {}}, "description": "PNG QR code"}},
)
async def ticket_qr(
    booking_ref: str,
    db: DbSession,
    user: OptionalUser = None,
    token: str | None = None,
) -> Response:
    booking = await get_booking_by_ref(db, booking_ref)
    ticket = await get_ticket(db, booking)
    if ticket is None:
        raise NotFoundError("No ticket has been issued for this booking yet.")

    authorized = False
    if user:
        if user.role in {UserRole.OPERATIONS, UserRole.SUPER_ADMIN, UserRole.DRIVER}:
            authorized = True
        elif booking.user_id and booking.user_id == user.id:
            authorized = True

    if not authorized and token:
        token_clean = token.strip()
        if token_clean == ticket.qr_token or constant_time_equals(token_clean, ticket.qr_signature):
            authorized = True
        elif token_clean.startswith("EJS1."):
            try:
                payload, sig = parse_qr_token(token_clean)
                if verify_ticket_signature(payload, sig) and payload.get("ref") == booking.booking_ref:
                    authorized = True
            except Exception:
                pass

    if not authorized:
        raise ForbiddenError("You do not have permission to view this ticket QR code.")

    png = read_ticket_png(ticket)
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="{booking.booking_ref}.png"',
        },
    )


@router.post(
    "/validate",
    response_model=ValidateTicketResponse,
    summary="Validate a scanned QR ticket and check the passenger in",
    description=(
        "Used by the admin and driver scanner pages. Verifies the ticket's HMAC "
        "signature, confirms it is for today's departure, then marks it checked in. "
        "A second scan of the same ticket reports `already_checked_in` rather than "
        "silently succeeding. Accepts a bare booking reference for manual entry."
    ),
)
async def validate_ticket(
    payload: ValidateTicketRequest, db: DbSession, staff: StaffUser, request: Request
) -> ValidateTicketResponse:
    outcome = await validate_and_check_in(
        db,
        payload.qr_token,
        checked_in_by=staff.id,
        expected_trip_id=payload.trip_id,
    )

    # Only successful boardings are logged. A mis-scan is noise, not an event.
    if outcome.valid and outcome.booking is not None:
        await audit_service.record(
            db, actor=staff, action="ticket.check_in", entity_type="booking",
            entity_id=outcome.booking.id, entity_label=outcome.booking.booking_ref,
            request=request,
            summary=(
                f"Checked in {outcome.booking.passenger_name} on {outcome.booking.booking_ref} "
                f"({outcome.booking.seats} seat(s))."
            ),
        )

    await db.commit()

    booking = outcome.booking
    trip_summary = None
    if booking is not None:
        trip = await db.get(Trip, booking.trip_id)
        if trip:
            route = await db.get(Route, trip.route_id)
            trip_summary = f"{route.name} · {fmt_datetime(trip.departure_datetime)}"

    return ValidateTicketResponse(
        valid=outcome.valid,
        status=outcome.status,
        message=outcome.message,
        booking_ref=booking.booking_ref if booking else None,
        passenger_name=booking.passenger_name if booking else None,
        seats=booking.seats if booking else None,
        seat_numbers=(booking.seat_numbers or []) if booking else [],
        trip_summary=trip_summary,
        checked_in_at=outcome.checked_in_at,
        already_checked_in=outcome.already_checked_in,
    )
