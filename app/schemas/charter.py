from __future__ import annotations

import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, EmailStr, Field

from app.schemas.booking import PaymentInit
from app.schemas.common import ORMModel, PhoneMixin


class CharterRequestCreate(PhoneMixin):
    """What a passenger submits on the charter form."""

    contact_name: str = Field(min_length=2, max_length=160)
    phone: str
    contact_email: EmailStr | None = None
    organisation: str | None = Field(default=None, max_length=160)

    route_id: uuid.UUID | None = Field(
        default=None, description="Optional — a charter may follow a published corridor"
    )
    origin_text: str = Field(min_length=2, max_length=200)
    destination_text: str = Field(min_length=2, max_length=200)
    service_date: date
    preferred_time: time | None = None
    passengers: int = Field(default=1, ge=1, le=60)
    return_trip: bool = False
    notes: str | None = Field(default=None, max_length=1000)


class CharterQuote(BaseModel):
    """Operations setting the binding price."""

    quoted_amount_kobo: int = Field(ge=0)
    quote_notes: str | None = Field(default=None, max_length=1000)
    vehicle_id: uuid.UUID | None = None
    notify: bool = True


class CharterAssign(BaseModel):
    vehicle_id: uuid.UUID
    driver_id: uuid.UUID | None = None
    #: Creates a Trip so the manifest, ticket and driver portal all work.
    create_trip: bool = True


class CharterDecline(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    notify: bool = True


class CharterRequestOut(ORMModel):
    id: uuid.UUID
    reference: str
    status: str

    contact_name: str
    contact_phone: str
    contact_email: str | None
    organisation: str | None

    route_id: uuid.UUID | None
    route_name: str | None = None
    origin_text: str
    destination_text: str
    service_date: date
    preferred_time: time | None
    passengers: int
    return_trip: bool
    notes: str | None

    quoted_amount_kobo: int | None
    quote_notes: str | None
    vehicle_id: uuid.UUID | None
    vehicle_name: str | None = None
    driver_id: uuid.UUID | None
    driver_name: str | None = None
    trip_id: uuid.UUID | None

    quoted_at: datetime | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    created_at: datetime


class CharterRequestResponse(BaseModel):
    charter: CharterRequestOut
    #: Present once the charter is quoted and awaiting payment.
    payment: PaymentInit | None = None
    indicative_amount_kobo: int | None = Field(
        default=None,
        description="Non-binding estimate shown on the form so nobody submits blind",
    )
    message: str


class CharterLookup(PhoneMixin):
    reference: str = Field(min_length=4, max_length=16)
    phone: str
