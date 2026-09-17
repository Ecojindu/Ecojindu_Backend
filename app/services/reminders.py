"""Schedule and deliver trip reminders via Cloud Tasks or APScheduler.

When CLOUD_TASKS_ENABLED is true, confirmation enqueues one HTTP task per
offset in REMINDER_OFFSETS_HOURS. Otherwise the APScheduler poller in
`app.jobs.scheduler` covers the same windows.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import log_event
from app.core.timeutil import now_utc
from app.models.booking import Booking
from app.models.enums import NotificationType
from app.models.route import Route
from app.models.trip import Trip
from app.services import notifications as notification_service

logger = logging.getLogger("ecojindu.reminders")


def window_label(hours: float) -> str:
    """Canonical window string used in notification types and job payloads."""
    if hours == int(hours):
        return f"{int(hours)}h"
    return f"{hours:g}h"


def notification_type_for_window(window: str) -> str:
    mapping = {
        "24h": NotificationType.REMINDER_24H,
        "3h": NotificationType.REMINDER_3H,
        "2h": NotificationType.REMINDER_2H,
        "1h": NotificationType.REMINDER_1H,
    }
    return mapping.get(window, f"reminder_{window}")


async def schedule_reminders(db: AsyncSession, booking: Booking, trip: Trip) -> list[dict]:
    """Enqueue (or log) reminder tasks for a newly confirmed booking.

    Returns a list of scheduled task descriptors for tests / logging.
    When Cloud Tasks is disabled this is a no-op beyond logging — APScheduler
    will pick the booking up by departure window.
    """
    offsets = settings.reminder_offsets_hours
    planned: list[dict] = []
    for hours in offsets:
        fire_at = trip.departure_datetime - timedelta(hours=hours)
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        window = window_label(hours)
        entry = {
            "booking_id": str(booking.id),
            "reminder_type": window,
            "fire_at": fire_at.isoformat(),
            "hours_before": hours,
        }
        if fire_at <= now_utc():
            entry["skipped"] = "already_past"
            planned.append(entry)
            continue

        if settings.CLOUD_TASKS_ENABLED:
            try:
                task_name = await enqueue_reminder_task(
                    booking_id=booking.id,
                    reminder_type=window,
                    schedule_time=fire_at,
                )
                entry["task_name"] = task_name
                entry["transport"] = "cloud_tasks"
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to enqueue cloud task for %s (%s)", booking.booking_ref, window
                )
                entry["transport"] = "cloud_tasks_failed"
        else:
            entry["transport"] = "apscheduler"
        planned.append(entry)

    log_event(
        logger,
        logging.INFO,
        "reminders scheduled",
        ref=booking.booking_ref,
        count=len(planned),
        cloud_tasks=settings.CLOUD_TASKS_ENABLED,
    )
    return planned


async def enqueue_reminder_task(
    *,
    booking_id: uuid.UUID,
    reminder_type: str,
    schedule_time: datetime,
) -> str:
    """Create an HTTP Cloud Task that POSTs to the internal send-reminder endpoint.

    Requires the optional `google-cloud-tasks` package when CLOUD_TASKS_ENABLED=true.
    """
    if not settings.CLOUD_TASKS_PROJECT or not settings.CLOUD_TASKS_SERVICE_URL:
        raise RuntimeError("CLOUD_TASKS_PROJECT and CLOUD_TASKS_SERVICE_URL are required")

    try:
        from google.cloud import tasks_v2
        from google.protobuf import timestamp_pb2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Install google-cloud-tasks to enable CLOUD_TASKS_ENABLED reminder delivery."
        ) from exc

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        settings.CLOUD_TASKS_PROJECT,
        settings.CLOUD_TASKS_LOCATION,
        settings.CLOUD_TASKS_QUEUE,
    )
    url = settings.CLOUD_TASKS_SERVICE_URL.rstrip("/") + "/v1/internal/jobs/send-reminder"
    body = json.dumps(
        {"booking_id": str(booking_id), "reminder_type": reminder_type}
    ).encode("utf-8")

    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(schedule_time.astimezone(timezone.utc))

    task: dict = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": {
                "Content-Type": "application/json",
                "X-Service-Key": settings.SERVICE_API_KEY,
            },
            "body": body,
        },
        "schedule_time": ts,
    }
    created = client.create_task(request={"parent": parent, "task": task})
    return created.name


async def send_reminder_for_booking(
    db: AsyncSession,
    *,
    booking_id: uuid.UUID,
    reminder_type: str,
) -> bool:
    """Deliver a single reminder (used by the Cloud Tasks internal endpoint)."""
    from sqlalchemy import and_, select
    from app.models.notification import Notification

    booking = await db.get(Booking, booking_id)
    if booking is None:
        log_event(logger, logging.WARNING, "reminder booking missing", booking_id=str(booking_id))
        return False

    trip = await db.get(Trip, booking.trip_id)
    if trip is None:
        return False
    route = await db.get(Route, trip.route_id)

    ntype = notification_type_for_window(reminder_type)
    already = (
        await db.execute(
            select(Notification.id).where(
                and_(Notification.booking_id == booking.id, Notification.type == ntype)
            ).limit(1)
        )
    ).scalar_one_or_none()
    if already is not None:
        return False

    await notification_service.send_trip_reminder(db, booking, trip, route, reminder_type)
    return True
