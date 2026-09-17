"""APScheduler background jobs.

| Job                    | Cadence            | What it does                                   |
|------------------------|--------------------|------------------------------------------------|
| release_holds          | every minute       | Returns seats from abandoned checkouts         |
| reminder_*             | every 10–15 min    | Emails + texts at each REMINDER_OFFSETS_HOURS  |
| generate_trips         | 00:20 daily        | Materialises the next 14 days of the timetable |
| subscription_hygiene   | 01:00 daily        | Expiry warnings and status transitions         |

Every job is idempotent, so a missed or repeated fire is harmless.
When CLOUD_TASKS_ENABLED is true, reminder delivery is driven by Cloud Tasks
instead; the poller jobs are still registered as a safety net.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import and_, select

from app.core.config import settings
from app.core.logging import log_event
from app.core.timeutil import LAGOS, now_utc
from app.db.session import session_scope
from app.models.booking import Booking
from app.models.enums import (
    BookingStatus,
    SubscriptionStatus,
    TripStatus,
)
from app.models.notification import Notification
from app.models.route import Route
from app.models.subscription import Subscription
from app.models.trip import Trip
from app.models.user import User
from app.services import notifications as notification_service
from app.services import payments as payment_service
from app.services.reminders import notification_type_for_window, window_label
from app.services.trips import complete_finished_trips, generate_trips_from_templates, release_expired_holds

logger = logging.getLogger("ecojindu.jobs")

scheduler: AsyncIOScheduler | None = None


async def job_release_holds() -> None:
    async with session_scope() as db:
        released = await release_expired_holds(db)
        if released:
            log_event(logger, logging.INFO, "job: released seat holds", seats=released)


async def _already_notified(db, booking_id, ntype: str) -> bool:
    row = (
        await db.execute(
            select(Notification.id).where(
                and_(Notification.booking_id == booking_id, Notification.type == ntype)
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _send_reminders(window: str, lead: timedelta, slack: timedelta) -> int:
    """Notify bookings whose departure falls inside [now+lead, now+lead+slack)."""
    ntype = notification_type_for_window(window)
    now = now_utc()
    lower, upper = now + lead, now + lead + slack

    sent = 0
    async with session_scope() as db:
        stmt = (
            select(Booking, Trip)
            .join(Trip, Trip.id == Booking.trip_id)
            .where(
                and_(
                    Trip.departure_datetime >= lower,
                    Trip.departure_datetime < upper,
                    Trip.status.in_([TripStatus.SCHEDULED, TripStatus.BOARDING]),
                    Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.CHECKED_IN]),
                )
            )
        )
        for booking, trip in (await db.execute(stmt)).unique().all():
            if await _already_notified(db, booking.id, ntype):
                continue
            route = await db.get(Route, trip.route_id)
            try:
                await notification_service.send_trip_reminder(db, booking, trip, route, window)
                sent += 1
            except Exception:  # noqa: BLE001 - one bad recipient must not stop the batch
                logger.exception("reminder failed for %s", booking.booking_ref)

    if sent:
        log_event(logger, logging.INFO, "job: reminders sent", window=window, count=sent)
    return sent


def _slack_for_hours(hours: float) -> timedelta:
    if hours >= 12:
        return timedelta(minutes=15)
    return timedelta(minutes=10)


async def job_reminder_offset(hours: float) -> None:
    window = window_label(hours)
    await _send_reminders(window, timedelta(hours=hours), _slack_for_hours(hours))


# Keep named entry-points for backwards compatibility / manual triggers.
async def job_reminder_24h() -> None:
    await job_reminder_offset(24)


async def job_reminder_2h() -> None:
    await job_reminder_offset(2)


async def job_generate_trips() -> None:
    async with session_scope() as db:
        created = await generate_trips_from_templates(db)
        completed = await complete_finished_trips(db)
        await payment_service.expire_stale_pending_payments(db)
    log_event(logger, logging.INFO, "job: timetable rollover", trips_created=created, bookings_completed=completed)


async def job_subscription_hygiene() -> None:
    """Expire lapsed subscriptions and warn subscribers 7 days out."""
    now = now_utc()
    async with session_scope() as db:
        expired = list(
            (
                await db.execute(
                    select(Subscription).where(
                        and_(
                            Subscription.status == SubscriptionStatus.ACTIVE,
                            Subscription.expires_at.is_not(None),
                            Subscription.expires_at < now,
                        )
                    )
                )
            ).unique().scalars().all()
        )
        for sub in expired:
            sub.status = SubscriptionStatus.EXPIRED

        warn_from, warn_to = now + timedelta(days=6), now + timedelta(days=7)
        expiring = list(
            (
                await db.execute(
                    select(Subscription).where(
                        and_(
                            Subscription.status == SubscriptionStatus.ACTIVE,
                            Subscription.expires_at >= warn_from,
                            Subscription.expires_at < warn_to,
                            Subscription.credits_used < Subscription.credits_total,
                        )
                    )
                )
            ).unique().scalars().all()
        )
        for sub in expiring:
            user = await db.get(User, sub.user_id)
            if user:
                try:
                    await notification_service.send_subscription_expiring(db, sub, user, 7)
                except Exception:  # noqa: BLE001
                    logger.exception("expiry warning failed for subscription %s", sub.id)

    log_event(
        logger, logging.INFO, "job: subscription hygiene", expired=len(expired), warned=len(expiring)
    )


def start_scheduler() -> AsyncIOScheduler | None:
    global scheduler
    if not settings.SCHEDULER_ENABLED:
        logger.info("scheduler disabled (SCHEDULER_ENABLED=false)")
        return None
    if scheduler is not None:
        return scheduler

    scheduler = AsyncIOScheduler(timezone=LAGOS)
    scheduler.add_job(job_release_holds, IntervalTrigger(minutes=1), id="release_holds", replace_existing=True)

    # One poller per configured reminder offset (24 / 3 / 1 by default).
    # Also keep the legacy 2h window so older bookings still get a final call.
    offsets = list(settings.reminder_offsets_hours)
    if 2.0 not in offsets and 2 not in offsets:
        offsets.append(2.0)
    for hours in offsets:
        job_id = f"reminder_{window_label(hours)}"
        interval = 15 if hours >= 12 else 10
        scheduler.add_job(
            job_reminder_offset,
            IntervalTrigger(minutes=interval),
            id=job_id,
            replace_existing=True,
            kwargs={"hours": float(hours)},
        )

    scheduler.add_job(
        job_generate_trips, CronTrigger(hour=0, minute=20), id="generate_trips", replace_existing=True
    )
    scheduler.add_job(
        job_subscription_hygiene, CronTrigger(hour=1, minute=0), id="subscription_hygiene", replace_existing=True
    )
    scheduler.start()
    log_event(logger, logging.INFO, "scheduler started", jobs=len(scheduler.get_jobs()))
    return scheduler


def shutdown_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("scheduler stopped")
