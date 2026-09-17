"""Flight status lookups."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.schemas.ticket_reading import FlightStatusOut
from app.services import flights as flight_service

router = APIRouter(prefix="/flights", tags=["Flights"])


@router.get(
    "/status",
    response_model=FlightStatusOut,
    summary="Live flight status (AviationStack when configured)",
)
async def flight_status(
    flight_number: str = Query(..., min_length=2, max_length=16),
    date: date | None = Query(None, description="Flight date YYYY-MM-DD (defaults to today)"),
) -> FlightStatusOut:
    result = await flight_service.get_flight_status(flight_number=flight_number, flight_date=date)
    return FlightStatusOut(**result)
