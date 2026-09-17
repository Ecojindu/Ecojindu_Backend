"""Flight status lookups via AviationStack (optional)."""
from __future__ import annotations

import logging
from datetime import date

import httpx

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger("ecojindu.flights")

AVIATIONSTACK_URL = "https://api.aviationstack.com/v1/flights"


def _normalise_flight_number(value: str) -> str:
    return value.strip().upper().replace(" ", "")


def _map_status(raw: str | None) -> tuple[str, str]:
    """Return (status, human message)."""
    key = (raw or "").strip().lower()
    mapping = {
        "scheduled": ("scheduled", "This flight is scheduled as planned."),
        "active": ("active", "This flight is currently airborne."),
        "landed": ("landed", "This flight has landed."),
        "cancelled": ("cancelled", "This flight has been cancelled."),
        "canceled": ("cancelled", "This flight has been cancelled."),
        "delayed": ("delayed", "This flight is delayed."),
        "incident": ("incident", "This flight has an operational incident."),
        "diverted": ("diverted", "This flight has been diverted."),
    }
    if key in mapping:
        return mapping[key]
    if "delay" in key:
        return "delayed", "This flight is delayed."
    if "cancel" in key:
        return "cancelled", "This flight has been cancelled."
    return "unknown", f"Flight status reported as '{raw or 'unknown'}'."


async def get_flight_status(*, flight_number: str, flight_date: date | None = None) -> dict:
    """Look up a flight. Without an API key, returns status=unknown."""
    number = _normalise_flight_number(flight_number)
    day = flight_date or date.today()

    if not settings.AVIATIONSTACK_API_KEY:
        return {
            "flight_number": number,
            "date": day.isoformat(),
            "status": "unknown",
            "message": "Flight status is unavailable. Configure AVIATIONSTACK_API_KEY to enable live updates.",
            "airline": None,
            "departure_airport": None,
            "departure_scheduled": None,
            "departure_estimated": None,
            "arrival_airport": None,
            "delay_minutes": None,
        }

    params = {
        "access_key": settings.AVIATIONSTACK_API_KEY,
        "flight_iata": number,
        "flight_date": day.isoformat(),
        "limit": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(AVIATIONSTACK_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log_event(logger, logging.WARNING, "aviationstack lookup failed", error=str(exc))
        return {
            "flight_number": number,
            "date": day.isoformat(),
            "status": "unknown",
            "message": "We could not reach the flight status provider right now.",
            "airline": None,
            "departure_airport": None,
            "departure_scheduled": None,
            "departure_estimated": None,
            "arrival_airport": None,
            "delay_minutes": None,
        }

    rows = payload.get("data") or []
    if not rows:
        return {
            "flight_number": number,
            "date": day.isoformat(),
            "status": "unknown",
            "message": "No matching flight was found for that number and date.",
            "airline": None,
            "departure_airport": None,
            "departure_scheduled": None,
            "departure_estimated": None,
            "arrival_airport": None,
            "delay_minutes": None,
        }

    row = rows[0]
    status, message = _map_status(row.get("flight_status"))
    dep = row.get("departure") or {}
    arr = row.get("arrival") or {}
    airline = (row.get("airline") or {}).get("name")
    delay = dep.get("delay")
    if status == "delayed" and isinstance(delay, (int, float)) and delay:
        message = f"This flight is delayed by about {int(delay)} minutes."

    return {
        "flight_number": number,
        "date": day.isoformat(),
        "status": status,
        "message": message,
        "airline": airline,
        "departure_airport": dep.get("iata") or dep.get("airport"),
        "departure_scheduled": dep.get("scheduled"),
        "departure_estimated": dep.get("estimated") or dep.get("actual"),
        "arrival_airport": arr.get("iata") or arr.get("airport"),
        "delay_minutes": int(delay) if isinstance(delay, (int, float)) else None,
    }
