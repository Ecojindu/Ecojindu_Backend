from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import BookingSource, BookingStatus

if TYPE_CHECKING:
    from app.models.trip import Trip
    from app.models.user import User


class Booking(Base, TimestampMixin):
    __tablename__ = "bookings"
    __table_args__ = (
        Index("ix_bookings_passenger_phone", "passenger_phone"),
        Index("ix_bookings_trip_status", "trip_id", "status"),
        # New bookings must account for every seat by sex (enforced in the API too).
        # Bookings taken before demographic collection existed sit at 0/0, and the
        # constraint permits that rather than fabricating data for them.
        CheckConstraint(
            "(seats_male = 0 AND seats_female = 0) OR (seats_male + seats_female = seats)",
            name="seats_by_sex_match_total",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_ref: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    #: Null for guest bookings — the site never forces a signup before paying.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trips.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    passenger_name: Mapped[str] = mapped_column(String(160), nullable=False)
    passenger_phone: Mapped[str] = mapped_column(String(24), nullable=False)
    passenger_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seats: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Demographic split, collected at search time for state reporting.
    seats_male: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    seats_female: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    seat_numbers: Mapped[list[str]] = mapped_column(ARRAY(String(8)), default=list, nullable=False)
    amount_kobo: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(24), default=BookingSource.WEB, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), default=BookingStatus.PENDING_PAYMENT, nullable=False, index=True
    )
    #: While pending payment the seats are held until this instant, then released.
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rescheduled_from_booking_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    pickup_stop_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("route_stops.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    trip: Mapped["Trip"] = relationship(back_populates="bookings", lazy="joined")
    user: Mapped["User | None"] = relationship(lazy="selectin")
    ticket: Mapped["Ticket | None"] = relationship(
        back_populates="booking", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Booking {self.booking_ref} {self.status}>"


class Ticket(Base):
    """A signed, scannable boarding pass. One per confirmed booking."""

    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    #: Base64url-encoded canonical JSON payload (the middle segment of the QR token).
    qr_payload: Mapped[str] = mapped_column(Text, nullable=False)
    qr_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Full `EJS1.<payload>.<sig>` string, exactly what the QR image encodes.
    qr_token: Mapped[str] = mapped_column(Text, nullable=False)
    qr_image_path: Mapped[str] = mapped_column(Text, nullable=False)
    qr_image_url: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_in_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    booking: Mapped["Booking"] = relationship(back_populates="ticket")
