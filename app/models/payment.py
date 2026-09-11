from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import PaymentStatus


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    charter_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("charter_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Named for Paystack historically; holds the reference for whichever
    #: provider PAYMENT_PROVIDER selects.
    paystack_reference: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(24), default="paystack", nullable=False)
    authorization_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    amount_kobo: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="NGN", nullable=False)
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), default=PaymentStatus.PENDING, nullable=False, index=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_webhook: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class WebhookEvent(Base):
    """Idempotency ledger.

    Every inbound webhook is recorded here under a stable key before it is
    processed; a duplicate delivery hits the unique index and is skipped.
    """

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False, index=True)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
