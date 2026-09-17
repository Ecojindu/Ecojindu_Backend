from __future__ import annotations

import uuid

from datetime import date
from sqlalchemy import Date, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import VehicleStatus


class Vehicle(Base, TimestampMixin):
    """An electric shuttle in the Ecojindu fleet."""

    __tablename__ = "vehicles"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    plate_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(120), default="Wuling EV Minibus", nullable=False)
    seat_capacity: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    range_km: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    battery_level_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odometer_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_maintenance_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=VehicleStatus.ACTIVE, nullable=False, index=True)
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
