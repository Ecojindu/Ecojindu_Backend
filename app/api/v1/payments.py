"""Paystack verification, webhook receiver, and the local mock checkout page."""
from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse

from app.api.deps import DbSession
from app.core.config import settings
from app.core.errors import AuthError
from app.core.security import verify_paystack_signature
from app.core.timeutil import naira
from app.models.enums import PaymentStatus
from app.schemas.booking import BookingOut
from app.schemas.common import Message
from app.services import payments as payment_service
from app.services.bookings import get_booking_by_ref

router = APIRouter(prefix="/payments", tags=["Payments"])


@router.get(
    "/verify/{reference}",
    summary="Verify a Paystack transaction and settle it",
    description=(
        "Called by the frontend once Paystack's inline checkout closes. Settling is "
        "idempotent, so it is safe to call alongside the webhook."
    ),
)
async def verify_payment(reference: str, db: DbSession) -> dict:
    payment = await payment_service.verify_and_settle(db, reference)
    await db.commit()

    booking_out = None
    if payment.booking_id:
        from app.models.booking import Booking

        booking = await db.get(Booking, payment.booking_id)
        if booking:
            booking_out = BookingOut.model_validate(booking)

    return {
        "reference": payment.paystack_reference,
        "status": payment.status,
        "amount_kobo": payment.amount_kobo,
        "paid": payment.status == PaymentStatus.SUCCESS,
        "booking_ref": booking_out.booking_ref if booking_out else None,
        "subscription_id": str(payment.subscription_id) if payment.subscription_id else None,
        "message": (
            "Payment confirmed. Your ticket has been emailed and texted to you."
            if payment.status == PaymentStatus.SUCCESS
            else "This payment has not been completed."
        ),
    }


@router.post(
    "/webhook/paystack",
    summary="Paystack webhook receiver (signature-verified, idempotent)",
    description=(
        "Verifies the `x-paystack-signature` HMAC-SHA512 header against the raw body, "
        "then processes the event exactly once via the `webhook_events` ledger."
    ),
)
async def paystack_webhook(
    request: Request,
    db: DbSession,
    x_paystack_signature: str | None = Header(default=None, alias="x-paystack-signature"),
) -> dict:
    raw = await request.body()

    # In mock mode there is no real secret to sign with, so the check is skipped
    # locally but always enforced once real keys are configured.
    if not settings.PAYSTACK_MOCK:
        if not verify_paystack_signature(raw, x_paystack_signature or ""):
            raise AuthError("Invalid webhook signature.", code="invalid_signature")

    body = await request.json()
    result = await payment_service.process_paystack_event(db, body)
    await db.commit()
    return result


@router.get(
    "/mock-pay/{reference}",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="Local stand-in for Paystack's hosted checkout",
)
async def mock_pay_page(reference: str, db: DbSession) -> HTMLResponse:
    payment = await payment_service.get_payment_by_reference(db, reference)
    amount = naira(payment.amount_kobo)
    already = payment.status == PaymentStatus.SUCCESS

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mock checkout · {reference}</title>
<style>
 body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
      background:#EAE8DB;display:grid;place-items:center;min-height:100vh;color:#15181A}}
 .card{{background:#fff;border-radius:22px;padding:36px;max-width:420px;width:92%;
        box-shadow:0 8px 40px rgba(47,82,51,.14)}}
 h1{{margin:0 0 6px;font-size:22px;letter-spacing:-.4px}}
 .tag{{display:inline-block;background:#F0F6EA;color:#4C8C2B;font-size:11px;font-weight:700;
       letter-spacing:1.4px;text-transform:uppercase;padding:6px 12px;border-radius:999px;margin-bottom:16px}}
 .amt{{font-size:40px;font-weight:800;color:#2F5233;margin:14px 0 4px}}
 .ref{{font-family:ui-monospace,Menlo,monospace;color:#8A918D;font-size:13px;margin-bottom:24px}}
 button{{width:100%;border:0;border-radius:999px;padding:16px;font-size:16px;font-weight:700;
         background:#4C8C2B;color:#fff;cursor:pointer}}
 button:disabled{{background:#B8C7B0;cursor:default}}
 .ok{{background:#EAF6F2;border:1px solid #C4E5DA;color:#1F7A63;padding:14px;border-radius:14px;
      font-weight:600;text-align:center}}
 p.note{{color:#8A918D;font-size:12.5px;line-height:1.6;margin-top:18px}}
</style></head><body><div class="card">
 <div class="tag">Sandbox · mock Paystack</div>
 <h1>Ecojindu Shuttle</h1>
 <div class="amt">{amount}</div>
 <div class="ref">{reference}</div>
 <div id="slot">
   {'<div class="ok">✓ Payment already completed</div>' if already else
    '<button id="pay" onclick="pay()">Pay ' + amount + '</button>'}
 </div>
 <p class="note">This page exists only because <code>PAYSTACK_MOCK=true</code>. Set real
 Paystack keys and <code>PAYSTACK_MOCK=false</code> to use the live checkout.</p>
</div>
<script>
async function pay(){{
  const b=document.getElementById('pay'); b.disabled=true; b.textContent='Processing…';
  const r=await fetch('{settings.PUBLIC_BASE_URL}/v1/payments/mock-pay/{reference}/complete',{{method:'POST'}});
  const d=await r.json();
  document.getElementById('slot').innerHTML = d.paid
    ? '<div class="ok">✓ Payment successful — ticket issued</div>'
    : '<div class="ok" style="background:#FBF3EE;border-color:#EBC9B7;color:#C4562F">Payment failed</div>';
  if(d.paid){{ setTimeout(()=>{{ location.href='{settings.PAYSTACK_CALLBACK_URL}?reference={reference}'; }}, 1400); }}
}}
</script></body></html>"""
    return HTMLResponse(html)


@router.post(
    "/mock-pay/{reference}/complete",
    include_in_schema=False,
    summary="Mark a mock payment successful",
)
async def mock_pay_complete(reference: str, db: DbSession) -> dict:
    payment = await payment_service.simulate_successful_payment(db, reference)
    await db.commit()
    return {"paid": payment.status == PaymentStatus.SUCCESS, "reference": reference}


@router.get("/{reference}", response_model=Message, summary="Payment status by reference")
async def payment_status(reference: str, db: DbSession) -> Message:
    payment = await payment_service.get_payment_by_reference(db, reference)
    return Message(message=payment.status, ok=payment.status == PaymentStatus.SUCCESS)
