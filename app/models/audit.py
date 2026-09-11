from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    """Append-only record of what staff and drivers changed.

    Deliberately denormalised: `actor_name` and `summary` are snapshotted at write
    time so the history still reads correctly after a driver is renamed or a route
    is deleted. There is no update or delete path anywhere in the API.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Snapshotted so the log survives the account being renamed or removed.
    actor_name: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    #: Dotted verb, e.g. `route.create`, `trip.cancel`, `ticket.check_in`.
    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Human-readable label for the thing acted on, e.g. "Umuahia → Sam Mbakwe Airport".
    entity_label: Mapped[str | None] = mapped_column(String(200), nullable=True)

    #: One sentence an operations manager can read without decoding the diff.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditLog {self.action} by {self.actor_name}>"
