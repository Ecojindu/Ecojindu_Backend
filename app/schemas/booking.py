from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.schemas.common import ORMModel, PhoneMixin
from app.schemas.trip import TripOut


class SexCountsMixin(BaseModel):
    """Passenger sex is collected at search time for state demographic reporting.

    When either count is supplied they must account for every seat. Channels that
    predate the field — or where the caller genuinely doesn't know, such as the
    WhatsApp bot — may omit both, which records the booking with no demographic data
    rather than inventing it.
    """

    seats_male: int = Field(default=0, ge=0, le=14)
    seats_female: int = Field(default=0, ge=0, le=14)

    @model_validator(mode="after")
    def _sex_counts_match_seats(self):
        total = self.seats_male + self.seats_female
        seats = getattr(self, "seats", None)
        if total and seats is not None and total != seats:
            raise ValueError(
                f"{total} passenger(s) by sex doesn't match {seats} seat(s) — "
                "please check the male and female counts."
            )
        return self


class BookingCreate(PhoneMixin, SexCountsMixin):
    trip_id: uuid.UUID
    passenger_name: str = Field(min_length=2, max_length=160)
    passenger_phone: str
    passenger_email: EmailStr | None = None
    seats: int = Field(default=1, ge=1, le=14)
    pickup_stop_id: uuid.UUID | None = None
    source: str = "web"
    notes: str | None = Field(default=None, max_length=500)


class AdminBookingCreate(BookingCreate):
    #: Admin-made bookings skip the payment step when marked as settled offline.
    mark_confirmed: bool = False
    amount_kobo_override: int | None = Field(default=None, ge=0)


class SubscriptionBookingCreate(PhoneMixin, SexCountsMixin):
    trip_id: uuid.UUID
    seats: int = Field(default=1, ge=1, le=14)
    passenger_name: str | None = None
    passenger_phone: str | None = None
    passenger_email: EmailStr | None = None
    pickup_stop_id: uuid.UUID | None = None


class InternalSubscriptionBooking(PhoneMixin, SexCountsMixin):
    """Credit booking made by a trusted service on a subscriber's behalf."""

    phone: str
    trip_id: uuid.UUID
    seats: int = Field(default=1, ge=1, le=14)
    passenger_name: str | None = None
    passenger_email: EmailStr | None = None
    source: str = "subscription"


class TicketOut(ORMModel):
    qr_token: str
    qr_image_url: str
    issued_at: datetime
    checked_in_at: datetime | None


class BookingOut(ORMModel):
    id: uuid.UUID
    booking_ref: str
    trip_id: uuid.UUID
    user_id: uuid.UUID | None
    subscription_id: uuid.UUID | None
    passenger_name: str
    passenger_phone: str
    passenger_email: str | None
    seats: int
    seats_male: int = 0
    seats_female: int = 0
    seat_numbers: list[str]
    amount_kobo: int
    source: str
    status: str
    hold_expires_at: datetime | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    trip: TripOut | None = None
    ticket: TicketOut | None = None


class PaymentInit(BaseModel):
    reference: str
    authorization_url: str
    access_code: str
    public_key: str
    amount_kobo: int
    email: str


class BookingCreateResponse(BaseModel):
    booking: BookingOut
    payment: PaymentInit | None = None
    hold_expires_at: datetime | None = None
    message: str


class BookingLookup(PhoneMixin):
    booking_ref: str = Field(min_length=4, max_length=16)
    phone: str


class CancelBookingRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ResendTicketRequest(BaseModel):
    channels: list[str] = Field(default=["email", "sms"])


class ValidateTicketRequest(BaseModel):
    #: Raw string decoded from the QR image, or a bare booking ref for manual entry.
    qr_token: str = Field(min_length=4)
    trip_id: uuid.UUID | None = None


class ValidateTicketResponse(BaseModel):
    valid: bool
    status: str
    message: str
    booking_ref: str | None = None
    passenger_name: str | None = None
    seats: int | None = None
    seat_numbers: list[str] = []
    trip_summary: str | None = None
    checked_in_at: datetime | None = None
    already_checked_in: bool = False
