"""Paystack client.

`PAYSTACK_MOCK=true` swaps in an in-process simulator so the entire booking →
payment → ticket → notification flow runs locally with no live keys. The mock
returns a `/mock-pay/<reference>` authorization URL that the backend serves,
and honours the same verify contract as the live API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.core.config import settings
from app.core.errors import PaymentError
from app.core.logging import log_event
from app.core.security import generate_payment_reference

logger = logging.getLogger("ecojindu.paystack")


@dataclass(slots=True)
class InitResult:
    reference: str
    authorization_url: str
    access_code: str


@dataclass(slots=True)
class VerifyResult:
    reference: str
    status: str  # success | failed | abandoned | pending
    amount_kobo: int
    currency: str
    channel: str | None
    paid_at: datetime | None
    customer_email: str | None
    raw: dict


class PaystackClient:
    def __init__(self) -> None:
        self.secret = settings.PAYSTACK_SECRET_KEY
        self.base = settings.PAYSTACK_BASE_URL.rstrip("/")
        self.mock = settings.PAYSTACK_MOCK or self.secret.endswith("placeholder")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
        }

    async def initialize(
        self,
        *,
        email: str,
        amount_kobo: int,
        reference: str | None = None,
        metadata: dict | None = None,
        callback_url: str | None = None,
    ) -> InitResult:
        reference = reference or generate_payment_reference()
        if amount_kobo <= 0:
            raise PaymentError("Payment amount must be greater than zero.")

        if self.mock:
            log_event(logger, logging.INFO, "Paystack init (mock)", reference=reference, amount_kobo=amount_kobo)
            return InitResult(
                reference=reference,
                authorization_url=f"{settings.PUBLIC_BASE_URL}/v1/payments/mock-pay/{reference}",
                access_code=f"mock_{reference[-10:].lower()}",
            )

        payload = {
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
            "currency": "NGN",
            "callback_url": callback_url or settings.PAYSTACK_CALLBACK_URL,
            "metadata": metadata or {},
        }
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(
                    f"{self.base}/transaction/initialize", json=payload, headers=self._headers
                )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise PaymentError("Could not reach Paystack. Please try again.") from exc

        if not data.get("status"):
            raise PaymentError(data.get("message") or "Paystack rejected the transaction.")

        body = data["data"]
        return InitResult(
            reference=body["reference"],
            authorization_url=body["authorization_url"],
            access_code=body["access_code"],
        )

    async def verify(self, reference: str) -> VerifyResult:
        if self.mock:
            # The mock checkout page marks payments successful via the backend,
            # so verify simply reports success for any reference it is given.
            log_event(logger, logging.INFO, "Paystack verify (mock)", reference=reference)
            return VerifyResult(
                reference=reference,
                status="success",
                amount_kobo=0,
                currency="NGN",
                channel="mock",
                paid_at=datetime.now(timezone.utc),
                customer_email=None,
                raw={"mock": True, "reference": reference},
            )

        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.get(
                    f"{self.base}/transaction/verify/{reference}", headers=self._headers
                )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise PaymentError("Could not reach Paystack to verify this payment.") from exc

        if not data.get("status"):
            raise PaymentError(data.get("message") or "Paystack could not verify that reference.")

        body = data["data"]
        paid_at = None
        if body.get("paid_at"):
            paid_at = datetime.fromisoformat(body["paid_at"].replace("Z", "+00:00"))

        return VerifyResult(
            reference=body["reference"],
            status=body.get("status", "failed"),
            amount_kobo=int(body.get("amount") or 0),
            currency=body.get("currency", "NGN"),
            channel=body.get("channel"),
            paid_at=paid_at,
            customer_email=(body.get("customer") or {}).get("email"),
            raw=body,
        )


paystack = PaystackClient()
