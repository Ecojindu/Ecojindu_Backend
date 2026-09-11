"""Charter hire — public request/lookup plus the operations quote queue."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import desc, select

from app.api.deps import AdminUser, DbSession, OptionalUser, ServiceOrAdmin
from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError
from app.core.ratelimit import RateLimiter
from app.models.charter import CharterRequest
from app.models.enums import CharterStatus
from app.schemas.booking import PaymentInit
from app.schemas.charter import (
    CharterAssign,
    CharterDecline,
    CharterLookup,
    CharterQuote,
    CharterRequestCreate,
    CharterRequestOut,
    CharterRequestResponse,
)
from app.schemas.common import Message
from app.services import audit as audit_service
from app.services import charter as charter_service
from app.services import payments as payment_service

router = APIRouter(prefix="/charter", tags=["Charter"])


def _out(charter: CharterRequest) -> CharterRequestOut:
    out = CharterRequestOut.model_validate(charter)
    out.route_name = charter.route_name
    out.vehicle_name = charter.vehicle_name
    out.driver_name = charter.driver_name
    return out


# ══════════════════════════════════════════════════════════════
#  Public
# ══════════════════════════════════════════════════════════════


@router.post(
    "/requests",
    response_model=CharterRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request a whole-vehicle charter",
    description=(
        "Submits a charter enquiry. Nothing is charged at this point — operations "
        "review the request and send back a binding price, which the customer then "
        "pays to confirm. The response carries a non-binding indicative amount so "
        "the customer isn't submitting blind."
    ),
    dependencies=[Depends(RateLimiter("charter_request", limit=10))],
)
async def request_charter(
    payload: CharterRequestCreate, db: DbSession, user: OptionalUser
) -> CharterRequestResponse:
    charter = await charter_service.create_request(
        db,
        contact_name=payload.contact_name,
        contact_phone=payload.phone,
        contact_email=payload.contact_email or (user.email if user else None),
        organisation=payload.organisation,
        route_id=payload.route_id,
        origin_text=payload.origin_text,
        destination_text=payload.destination_text,
        service_date=payload.service_date,
        preferred_time=payload.preferred_time,
        passengers=payload.passengers,
        return_trip=payload.return_trip,
        notes=payload.notes,
    )
    estimate = await charter_service.indicative_amount(db, payload.route_id, payload.passengers)
    await db.commit()
    await db.refresh(charter)

    return CharterRequestResponse(
        charter=_out(charter),
        payment=None,
        indicative_amount_kobo=estimate,
        message=(
            f"Charter {charter.reference} received. Our team will send you a price "
            "within one working day — nothing is charged until you accept it."
        ),
    )


@router.post(
    "/lookup",
    response_model=CharterRequestResponse,
    summary="Look up a charter by reference and phone number",
)
async def lookup_charter(payload: CharterLookup, db: DbSession) -> CharterRequestResponse:
    charter = await charter_service.lookup(db, payload.reference, payload.phone)
    payment = None

    # A quoted charter needs a payment link so it can be confirmed.
    if charter.status == CharterStatus.QUOTED and charter.quoted_amount_kobo:
        record = await payment_service.initialize_charter_payment(db, charter)
        await db.commit()
        payment = PaymentInit(
            reference=record.paystack_reference,
            authorization_url=record.authorization_url or "",
            access_code=record.access_code or "",
            public_key=settings.PAYSTACK_PUBLIC_KEY,
            amount_kobo=record.amount_kobo,
            email=record.customer_email or "",
        )

    return CharterRequestResponse(
        charter=_out(charter),
        payment=payment,
        indicative_amount_kobo=None,
        message=_status_message(charter),
    )


def _status_message(charter: CharterRequest) -> str:
    return {
        CharterStatus.REQUESTED: "We're preparing your quote. You'll hear from us within one working day.",
        CharterStatus.QUOTED: "Your quote is ready. Pay to confirm the vehicle.",
        CharterStatus.CONFIRMED: "Paid and confirmed. We'll send your vehicle and driver details before travel.",
        CharterStatus.ASSIGNED: "Your vehicle and driver are assigned.",
        CharterStatus.COMPLETED: "This charter has been travelled.",
        CharterStatus.CANCELLED: "This charter was cancelled.",
        CharterStatus.DECLINED: "We weren't able to take this charter.",
    }.get(CharterStatus(charter.status), "Charter found.")


# ══════════════════════════════════════════════════════════════
#  Operations
# ══════════════════════════════════════════════════════════════


@router.get(
    "/admin/requests",
    response_model=list[CharterRequestOut],
    summary="The charter queue",
)
async def list_charters(
    db: DbSession,
    admin: AdminUser,
    charter_status: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
) -> list[CharterRequestOut]:
    stmt = select(CharterRequest).order_by(desc(CharterRequest.created_at)).limit(limit)
    if charter_status:
        stmt = stmt.where(CharterRequest.status == charter_status)
    rows = list((await db.execute(stmt)).unique().scalars().all())
    return [_out(c) for c in rows]


@router.get("/admin/requests/{reference}", response_model=CharterRequestOut)
async def get_charter(reference: str, db: DbSession, admin: AdminUser) -> CharterRequestOut:
    return _out(await charter_service.get_by_reference(db, reference))


@router.post(
    "/admin/requests/{reference}/quote",
    response_model=CharterRequestOut,
    summary="Send the customer a binding price",
)
async def quote_charter(
    reference: str, payload: CharterQuote, db: DbSession, admin: AdminUser, request: Request
) -> CharterRequestOut:
    charter = await charter_service.get_by_reference(db, reference)
    await charter_service.quote(
        db,
        charter,
        amount_kobo=payload.quoted_amount_kobo,
        quote_notes=payload.quote_notes,
        vehicle_id=payload.vehicle_id,
        quoted_by=admin.id,
        notify=payload.notify,
    )
    await audit_service.record(
        db, actor=admin, action="charter.quote", entity_type="charter",
        entity_id=charter.id, entity_label=charter.reference, request=request,
        summary=(
            f"Quoted {charter.reference} at "
            f"₦{payload.quoted_amount_kobo / 100:,.0f} for {charter.passengers} passengers"
            + (" and sent it to the customer." if payload.notify else " without notifying.")
        ),
    )
    await db.commit()
    await db.refresh(charter)
    return _out(charter)


@router.post(
    "/admin/requests/{reference}/assign",
    response_model=CharterRequestOut,
    summary="Assign a vehicle and driver, and create the trip",
    description=(
        "Only valid once the charter is paid for. Creating the trip is what makes the "
        "manifest, QR ticket and driver portal work for a charter exactly as they do "
        "for a scheduled departure."
    ),
)
async def assign_charter(
    reference: str, payload: CharterAssign, db: DbSession, admin: AdminUser, request: Request
) -> CharterRequestOut:
    charter = await charter_service.get_by_reference(db, reference)
    await charter_service.assign(
        db,
        charter,
        vehicle_id=payload.vehicle_id,
        driver_id=payload.driver_id,
        create_trip=payload.create_trip,
    )
    await audit_service.record(
        db, actor=admin, action="charter.assign", entity_type="charter",
        entity_id=charter.id, entity_label=charter.reference, request=request,
        summary=(
            f"Assigned {charter.vehicle_name} to {charter.reference}"
            + (f", driven by {charter.driver_name}." if charter.driver_name else ".")
        ),
    )
    await db.commit()
    await db.refresh(charter)
    return _out(charter)


@router.post(
    "/admin/requests/{reference}/decline",
    response_model=Message,
    summary="Decline or cancel a charter",
)
async def decline_charter(
    reference: str, payload: CharterDecline, db: DbSession, admin: AdminUser, request: Request
) -> Message:
    charter = await charter_service.get_by_reference(db, reference)
    declined = charter.status in {CharterStatus.REQUESTED, CharterStatus.QUOTED}
    await charter_service.cancel(
        db, charter, reason=payload.reason, declined=declined, notify=payload.notify
    )
    await audit_service.record(
        db, actor=admin, action="charter.decline" if declined else "charter.cancel",
        entity_type="charter", entity_id=charter.id, entity_label=charter.reference,
        request=request,
        summary=f"{'Declined' if declined else 'Cancelled'} {charter.reference} — {payload.reason}.",
    )
    await db.commit()
    return Message(
        message=f"{charter.reference} {'declined' if declined else 'cancelled'}."
        + (" The customer has been notified." if payload.notify else "")
    )


@router.post(
    "/admin/requests/{reference}/mark-paid",
    response_model=CharterRequestOut,
    summary="Record an off-platform payment (bank transfer, invoice)",
)
async def mark_charter_paid(
    reference: str, db: DbSession, admin: AdminUser
) -> CharterRequestOut:
    charter = await charter_service.get_by_reference(db, reference)
    if charter.quoted_amount_kobo is None:
        raise ConflictError("Quote this charter before marking it paid.")
    await charter_service.mark_confirmed(db, charter)
    await db.commit()
    await db.refresh(charter)
    return _out(charter)


# ══════════════════════════════════════════════════════════════
#  Service-to-service (WhatsApp bot / AI agent)
# ══════════════════════════════════════════════════════════════


@router.post(
    "/internal/requests",
    response_model=CharterRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Service-to-service charter request",
)
async def request_charter_internal(
    payload: CharterRequestCreate, db: DbSession, caller: ServiceOrAdmin
) -> CharterRequestResponse:
    charter = await charter_service.create_request(
        db,
        contact_name=payload.contact_name,
        contact_phone=payload.phone,
        contact_email=payload.contact_email,
        organisation=payload.organisation,
        route_id=payload.route_id,
        origin_text=payload.origin_text,
        destination_text=payload.destination_text,
        service_date=payload.service_date,
        preferred_time=payload.preferred_time,
        passengers=payload.passengers,
        return_trip=payload.return_trip,
        notes=payload.notes,
    )
    estimate = await charter_service.indicative_amount(db, payload.route_id, payload.passengers)
    await db.commit()
    await db.refresh(charter)
    return CharterRequestResponse(
        charter=_out(charter),
        payment=None,
        indicative_amount_kobo=estimate,
        message=f"Charter {charter.reference} received. Operations will quote within one working day.",
    )


@router.get(
    "/internal/requests/{reference}",
    response_model=CharterRequestOut,
    summary="Service-to-service charter lookup",
)
async def get_charter_internal(
    reference: str, db: DbSession, caller: ServiceOrAdmin
) -> CharterRequestOut:
    return _out(await charter_service.get_by_reference(db, reference))
