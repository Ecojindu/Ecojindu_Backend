"""Everything the passenger sees is in West Africa Time."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

LAGOS = ZoneInfo("Africa/Lagos")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_lagos() -> datetime:
    return datetime.now(LAGOS)


def today_lagos() -> date:
    return now_lagos().date()


def to_lagos(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LAGOS)


def combine_lagos(day: date, at: time) -> datetime:
    """Build a timezone-aware departure instant from a service date + timetable slot."""
    return datetime.combine(day, at).replace(tzinfo=LAGOS)


def fmt_time(dt: datetime) -> str:
    return to_lagos(dt).strftime("%I:%M %p").lstrip("0")


def fmt_date(dt: datetime | date) -> str:
    if isinstance(dt, datetime):
        dt = to_lagos(dt).date()
    return dt.strftime("%a, %d %b %Y")


def fmt_datetime(dt: datetime) -> str:
    local = to_lagos(dt)
    return f"{local.strftime('%a, %d %b %Y')} at {fmt_time(local)}"


def naira(kobo: int | None) -> str:
    return "₦{:,.0f}".format((kobo or 0) / 100)
