from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class TripOut(ORMModel):
    id: uuid.UUID
    route_id: uuid.UUID
    route_name: str
    origin_terminal: str
    destination: str
    service_date: date
    departure_datetime: datetime
    arrival_estimate: datetime | None
    duration_mins: int
    status: str
    seats_total: int
    seats_booked: int
    seats_available: int
    fare_kobo: int
    vehicle_id: uuid.UUID | None = None
    vehicle_name: str | None = None
    vehicle_model: str | None = None
    driver_id: uuid.UUID | None = None
    driver_name: str | None = None
    is_bookable: bool = True


class TripAdminOut(TripOut):
    template_id: uuid.UUID | None = None
    cancellation_reason: str | None = None
    revenue_kobo: int = 0
    confirmed_bookings: int = 0


class TripCreate(BaseModel):
    route_id: uuid.UUID
    departure_datetime: datetime
    vehicle_id: uuid.UUID | None = None
    driver_id: uuid.UUID | None = None
    seats_total: int | None = Field(default=None, ge=1, le=60)
    fare_kobo: int | None = Field(default=None, ge=0)


class TripUpdate(BaseModel):
    departure_datetime: datetime | None = None
    vehicle_id: uuid.UUID | None = None
    driver_id: uuid.UUID | None = None
    seats_total: int | None = Field(default=None, ge=1, le=60)
    fare_kobo: int | None = Field(default=None, ge=0)
    status: str | None = None
    notify_passengers: bool = False
    message: str | None = None


class TripStatusUpdate(BaseModel):
    status: str
    notify_passengers: bool = False
    reason: str | None = None


class TripCancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    notify_passengers: bool = True


class ManifestPassenger(BaseModel):
    booking_ref: str
    passenger_name: str
    passenger_phone: str
    passenger_email: str | None
    seats: int
    seat_numbers: list[str]
    status: str
    source: str
    amount_kobo: int
    checked_in_at: datetime | None = None
    pickup_stop: str | None = None


class TripManifest(BaseModel):
    trip: TripOut
    passengers: list[ManifestPassenger]
    total_passengers: int
    checked_in_count: int
    revenue_kobo: int


class SearchTripsQuery(BaseModel):
    route_id: uuid.UUID | None = None
    origin: str | None = None
    destination: str | None = None
    service_date: date | None = None
    seats: int = Field(default=1, ge=1, le=14)
