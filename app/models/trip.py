from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import TripStatus

if TYPE_CHECKING:
    from app.models.booking import Booking
    from app.models.fleet import Vehicle
    from app.models.route import Route
    from app.models.user import Driver


class TripTemplate(Base, TimestampMixin):
    """A recurring timetable entry — the airline-style schedule.

    `days_of_week` holds ISO weekday numbers (1 = Monday … 7 = Sunday).
    The nightly generation job materialises `Trip` rows from these.
    """

    __tablename__ = "trip_templates"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    route_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    departure_time: Mapped[time] = mapped_column(Time, nullable=False)
    days_of_week: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=lambda: [1, 2, 3, 4, 5, 6, 7], nullable=False
    )
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True
    )
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True
    )
    fare_override_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    route: Mapped["Route"] = relationship(lazy="joined")
    vehicle: Mapped["Vehicle | None"] = relationship(lazy="joined")
    driver: Mapped["Driver | None"] = relationship(lazy="joined")


class Trip(Base, TimestampMixin):
    """A concrete, bookable departure on a specific date."""

    __tablename__ = "trips"
    __table_args__ = (
        UniqueConstraint(
            "template_id", "service_date", name="uq_trips_template_id_service_date"
        ),
        Index("ix_trips_route_service_date", "route_id", "service_date"),
        Index("ix_trips_departure_datetime", "departure_datetime"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trip_templates.id", ondelete="SET NULL"), nullable=True
    )
    route_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    departure_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arrival_estimate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True
    )
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(24), default=TripStatus.SCHEDULED, nullable=False, index=True
    )
    seats_total: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    #: Includes both confirmed seats and unexpired payment holds.
    seats_booked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fare_kobo: Mapped[int] = mapped_column(Integer, nullable=False)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    route: Mapped["Route"] = relationship(lazy="joined")
    vehicle: Mapped["Vehicle | None"] = relationship(lazy="joined")
    driver: Mapped["Driver | None"] = relationship(back_populates="trips", lazy="joined")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="trip")

    # ── Derived attributes ────────────────────────────────────
    # These live on the model so a Trip can be serialised straight into TripOut,
    # including when it is reached through Booking.trip.

    @property
    def seats_available(self) -> int:
        return max(self.seats_total - self.seats_booked, 0)

    @property
    def route_name(self) -> str:
        return self.route.name if self.route else ""

    @property
    def origin_terminal(self) -> str:
        return self.route.origin_terminal if self.route else ""

    @property
    def destination(self) -> str:
        return self.route.destination if self.route else ""

    @property
    def duration_mins(self) -> int:
        return self.route.duration_mins if self.route else 0

    @property
    def vehicle_name(self) -> str | None:
        return self.vehicle.name if self.vehicle else None

    @property
    def vehicle_model(self) -> str | None:
        return self.vehicle.model if self.vehicle else None

    @property
    def driver_name(self) -> str | None:
        if self.driver is not None and self.driver.user is not None:
            return self.driver.user.full_name
        return None

    @property
    def is_bookable(self) -> bool:
        from datetime import datetime, timezone

        return (
            self.status == TripStatus.SCHEDULED
            and self.seats_available > 0
            and self.departure_datetime > datetime.now(timezone.utc)
        )
