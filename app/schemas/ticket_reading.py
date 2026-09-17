"""Schemas for Upload & Go ticket reading and shuttle matching."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.trip import TripOut


class TicketExtractionOut(BaseModel):
    passenger_names: list[str] = Field(default_factory=list)
    airline: str | None = None
    flight_number: str | None = None
    departure_airport: str | None = None
    departure_datetime: str | None = None
    arrival_airport: str | None = None
    arrival_datetime: str | None = None
    pnr: str | None = None


class ShuttleSuggestionOut(BaseModel):
    trip: TripOut | None = None
    fits: bool
    check_in_by: str
    message: str
    alternatives: list[TripOut] = Field(default_factory=list)


class TicketReadingResponse(BaseModel):
    extraction: TicketExtractionOut
    suggestion: ShuttleSuggestionOut
    privacy_note: str = (
        "Uploaded ticket was deleted after reading. We keep only the fields shown."
    )


class TicketMatchRequest(BaseModel):
    departure_datetime: datetime
    pickup_city: Literal["Umuahia", "Aba"] = "Umuahia"


class FlightStatusOut(BaseModel):
    flight_number: str
    date: str
    status: str
    message: str
    airline: str | None = None
    departure_airport: str | None = None
    departure_scheduled: str | None = None
    departure_estimated: str | None = None
    arrival_airport: str | None = None
    delay_minutes: int | None = None


class SendReminderRequest(BaseModel):
    booking_id: str
    reminder_type: str = Field(description="e.g. 24h, 3h, 1h")
