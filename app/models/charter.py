from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, Time
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import CharterStatus

if TYPE_CHECKING:
    from app.models.fleet import Vehicle
    from app.models.route import Route
    from app.models.trip import Trip
    from app.models.user import Driver


class CharterRequest(Base, TimestampMixin):
    """A whole-vehicle hire.

    Deliberately separate from `trips`: a charter is a bespoke departure rather
    than a timetable slot, and it is priced per vehicle rather than per seat.
    Once it is paid for, operations assigns a vehicle and a `Trip` is created —
    so the manifest, QR ticket and driver portal all work unchanged from there.
    """

    __tablename__ = "charter_requests"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), default=CharterStatus.REQUESTED, nullable=False, index=True
    )

    # ── Who's asking ──
    contact_name: Mapped[str] = mapped_column(String(160), nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    organisation: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # ── What they want ──
    #: Optional: a charter may follow a published corridor, or be entirely bespoke.
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    origin_text: Mapped[str] = mapped_column(String(200), nullable=False)
    destination_text: Mapped[str] = mapped_column(String(200), nullable=False)
    service_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    preferred_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    passengers: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    return_trip: Mapped[bool] = mapped_column(default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── What operations decided ──
    quoted_amount_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    quoted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True
    )
    driver_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True
    )
    #: Created once the charter is paid for and a vehicle is assigned.
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trips.id", ondelete="SET NULL"), nullable=True
    )

    quoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    route: Mapped["Route | None"] = relationship(lazy="joined")
    vehicle: Mapped["Vehicle | None"] = relationship(lazy="joined")
    driver: Mapped["Driver | None"] = relationship(lazy="joined")
    trip: Mapped["Trip | None"] = relationship(lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CharterRequest {self.reference} {self.status}>"

    @property
    def driver_name(self) -> str | None:
        if self.driver is not None and self.driver.user is not None:
            return self.driver.user.full_name
        return None

    @property
    def vehicle_name(self) -> str | None:
        return self.vehicle.name if self.vehicle else None

    @property
    def route_name(self) -> str | None:
        return self.route.name if self.route else None
