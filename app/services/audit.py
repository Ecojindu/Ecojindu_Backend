"""Audit trail for staff and driver actions.

Recording is deliberately explicit at each call site rather than inferred by
middleware: a generated summary like "PATCH /v1/admin/trips/3f2c…" tells an
operations manager nothing, whereas "Cancelled the 09:00 Umuahia → Sam Mbakwe
departure (14 passengers notified)" tells them everything.

Failures here never propagate. An audit write must not be able to roll back the
business action it is describing.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_ctx
from app.core.timeutil import now_utc
from app.models.audit import AuditLog
from app.models.user import User

logger = logging.getLogger("ecojindu.audit")


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    """Only the fields that actually changed, as `{field: [old, new]}`."""
    if not before or not after:
        return None
    changed = {
        key: [before.get(key), after[key]]
        for key in after
        if key in before and before.get(key) != after[key]
    }
    return changed or None


async def record(
    db: AsyncSession,
    *,
    actor: User | None,
    action: str,
    entity_type: str,
    summary: str,
    entity_id: str | uuid.UUID | None = None,
    entity_label: str | None = None,
    changes: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    """Append one entry. Never raises."""
    try:
        db.add(
            AuditLog(
                actor_user_id=actor.id if actor else None,
                actor_name=actor.full_name if actor else "System",
                actor_role=actor.role if actor else "system",
                action=action,
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id else None,
                entity_label=(entity_label or "")[:200] or None,
                summary=summary,
                changes=changes,
                ip=_client_ip(request),
                user_agent=(request.headers.get("user-agent") if request else None or "")[:300]
                or None,
                request_id=request_id_ctx.get(),
                created_at=now_utc(),
            )
        )
        await db.flush()
    except Exception:  # noqa: BLE001 - the audit trail must never break the action
        logger.exception("failed to write audit entry for %s", action)
