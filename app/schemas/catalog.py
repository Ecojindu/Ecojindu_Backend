"""Routes, stops, vehicles and drivers — the operator's catalogue."""
from __future__ import annotations

import uuid
from datetime import time

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel, PhoneMixin


# ── Route stops ──────────────────────────────────────────────
class RouteStopIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    lat: float | None = None
    lng: float | None = None
    order: int = Field(ge=0)
    pickup_allowed: bool = True


class RouteStopOut(ORMModel):
    id: uuid.UUID
    name: str
    lat: float | None
    lng: float | None
    order: int
    pickup_allowed: bool


# ── Routes ───────────────────────────────────────────────────
class RouteIn(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    code: str = Field(min_length=2, max_length=24)
    origin_terminal: str = Field(min_length=2, max_length=160)
    destination: str = Field(min_length=2, max_length=160)
    distance_km: float = Field(default=0, ge=0)
    duration_mins: int = Field(default=60, ge=5, le=600)
    base_fare_kobo: int = Field(ge=0)
    service_type: str = Field(default="airport", pattern="^(airport|rail|charter)$")
    charter_fare_kobo: int | None = Field(default=None, ge=0)
    is_active: bool = True
    description: str | None = None


class RouteUpdate(BaseModel):
    name: str | None = None
    origin_terminal: str | None = None
    destination: str | None = None
    distance_km: float | None = Field(default=None, ge=0)
    duration_mins: int | None = Field(default=None, ge=5, le=600)
    base_fare_kobo: int | None = Field(default=None, ge=0)
    service_type: str | None = Field(default=None, pattern="^(airport|rail|charter)$")
    charter_fare_kobo: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    description: str | None = None


class RouteOut(ORMModel):
    id: uuid.UUID
    name: str
    code: str
    origin_terminal: str
    destination: str
    distance_km: float
    duration_mins: int
    base_fare_kobo: int
    service_type: str
    charter_fare_kobo: int | None
    is_active: bool
    description: str | None
    stops: list[RouteStopOut] = []


class RouteAdminOut(RouteOut):
    """Adds the answer to "is this route actually sellable?"

    Creating a route isn't enough to put it on the website: it also needs an
    active timetable entry and generated departures. Surfacing that here stops
    an admin adding a route, seeing it appear, and then fielding complaints that
    it has no departures.
    """

    template_count: int = 0
    active_template_count: int = 0
    upcoming_trip_count: int = 0
    is_bookable: bool = False
    readiness: str = "not_ready"
    readiness_hint: str | None = None


# ── Vehicles ─────────────────────────────────────────────────
class VehicleIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    plate_no: str = Field(min_length=3, max_length=32)
    model: str = "Wuling EV Minibus"
    seat_capacity: int = Field(default=14, ge=1, le=60)
    range_km: int = Field(default=300, ge=0)
    status: str = "active"
    photo_url: str | None = None
    notes: str | None = None


class VehicleUpdate(BaseModel):
    name: str | None = None
    plate_no: str | None = None
    model: str | None = None
    seat_capacity: int | None = Field(default=None, ge=1, le=60)
    range_km: int | None = Field(default=None, ge=0)
    status: str | None = None
    photo_url: str | None = None
    notes: str | None = None


class VehicleOut(ORMModel):
    id: uuid.UUID
    name: str
    plate_no: str
    model: str
    seat_capacity: int
    range_km: int
    status: str
    photo_url: str | None
    notes: str | None


# ── Drivers ──────────────────────────────────────────────────
class DriverIn(PhoneMixin):
    full_name: str = Field(min_length=2, max_length=160)
    phone: str
    email: EmailStr | None = None
    password: str = Field(min_length=8, max_length=128)
    license_no: str = Field(min_length=3, max_length=64)
    photo_url: str | None = None
    assigned_vehicle_id: uuid.UUID | None = None
    status: str = "active"


class DriverUpdate(BaseModel):
    full_name: str | None = None
    email: EmailStr | None = None
    license_no: str | None = None
    photo_url: str | None = None
    assigned_vehicle_id: uuid.UUID | None = None
    status: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


class DriverOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    phone: str
    email: str | None
    license_no: str
    photo_url: str | None
    assigned_vehicle_id: uuid.UUID | None
    assigned_vehicle_name: str | None = None
    status: str
    is_active: bool = True


# ── Timetable templates ──────────────────────────────────────
class TripTemplateIn(BaseModel):
    route_id: uuid.UUID
    departure_time: time
    days_of_week: list[int] = Field(default=[1, 2, 3, 4, 5, 6, 7])
    vehicle_id: uuid.UUID | None = None
    driver_id: uuid.UUID | None = None
    fare_override_kobo: int | None = Field(default=None, ge=0)
    is_active: bool = True


class TripTemplateUpdate(BaseModel):
    departure_time: time | None = None
    days_of_week: list[int] | None = None
    vehicle_id: uuid.UUID | None = None
    driver_id: uuid.UUID | None = None
    fare_override_kobo: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class TripTemplateOut(ORMModel):
    id: uuid.UUID
    route_id: uuid.UUID
    route_name: str | None = None
    departure_time: time
    days_of_week: list[int]
    vehicle_id: uuid.UUID | None
    vehicle_name: str | None = None
    driver_id: uuid.UUID | None
    driver_name: str | None = None
    fare_override_kobo: int | None
    is_active: bool
