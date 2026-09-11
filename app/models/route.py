from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import ServiceType


class Route(Base, TimestampMixin):
    """A scheduled corridor, e.g. "Umuahia -> Sam Mbakwe Airport"."""

    __tablename__ = "routes"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    origin_terminal: Mapped[str] = mapped_column(String(160), nullable=False)
    destination: Mapped[str] = mapped_column(String(160), nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_mins: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    base_fare_kobo: Mapped[int] = mapped_column(Integer, nullable=False)
    #: What the route sells: seats on a timetable, or a whole vehicle.
    service_type: Mapped[str] = mapped_column(
        String(16), default=ServiceType.AIRPORT, nullable=False, index=True
    )
    #: Indicative whole-vehicle price shown on the charter form. The binding
    #: number is still the one operations quotes.
    charter_fare_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # selectin, not lazy: a Route is almost always serialised with its stops, and
    # a lazy load would fail outright inside async request handlers.
    stops: Mapped[list["RouteStop"]] = relationship(
        back_populates="route",
        cascade="all, delete-orphan",
        order_by="RouteStop.order",
        lazy="selectin",
    )


class RouteStop(Base, TimestampMixin):
    __tablename__ = "route_stops"
    __table_args__ = (UniqueConstraint("route_id", "order", name="uq_route_stops_route_id_order"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    route_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    pickup_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    route: Mapped["Route"] = relationship(back_populates="stops")
