from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.booking import PaymentInit
from app.schemas.common import ORMModel, PhoneMixin


class PlanIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    code: str = Field(min_length=2, max_length=32)
    price_kobo: int = Field(ge=0)
    ride_credits: int = Field(ge=1)
    validity_days: int = Field(ge=1)
    description: str | None = None
    perks: str | None = None
    is_active: bool = True
    sort_order: int = 0


class PlanUpdate(BaseModel):
    name: str | None = None
    price_kobo: int | None = Field(default=None, ge=0)
    ride_credits: int | None = Field(default=None, ge=1)
    validity_days: int | None = Field(default=None, ge=1)
    description: str | None = None
    perks: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class PlanOut(ORMModel):
    id: uuid.UUID
    name: str
    code: str
    price_kobo: int
    ride_credits: int
    validity_days: int
    description: str | None
    perks: str | None
    is_active: bool
    sort_order: int


class SubscriptionPurchase(PhoneMixin):
    plan_id: uuid.UUID
    full_name: str = Field(min_length=2, max_length=160)
    phone: str
    email: EmailStr


class SubscriptionOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    plan_id: uuid.UUID
    plan_name: str | None = None
    subscriber_name: str | None = None
    subscriber_phone: str | None = None
    credits_total: int
    credits_used: int
    credits_remaining: int
    starts_at: datetime | None
    expires_at: datetime | None
    status: str
    amount_paid_kobo: int
    created_at: datetime


class SubscriptionPurchaseResponse(BaseModel):
    subscription: SubscriptionOut
    payment: PaymentInit
    message: str
