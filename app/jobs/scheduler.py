"""APScheduler background jobs.

Five jobs run inside the backend process:

| Job                    | Cadence            | What it does                                   |
|------------------------|--------------------|------------------------------------------------|
| release_holds          | every minute       | Returns seats from abandoned checkouts         |
| reminder_24h           | every 15 minutes   | Emails + texts passengers a day before travel  |
| reminder_2h            | every 10 minutes   | Final call before departure                    |
| generate_trips         | 00:20 daily        | Materialises the next 14 days of the timetable |
| subscription_hygiene   | 01:00 daily        | Expiry warnings and status transitions         |

Every job is idempotent, so a missed or repeated fire is harmless.
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
    NotificationType,
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
    ntype = NotificationType.REMINDER_24H if window == "24h" else NotificationType.REMINDER_2H
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


async def job_reminder_24h() -> None:
    # Fires every 15 minutes with a 15-minute window, so each booking is caught once.
    await _send_reminders("24h", timedelta(hours=24), timedelta(minutes=15))


async def job_reminder_2h() -> None:
    await _send_reminders("2h", timedelta(hours=2), timedelta(minutes=10))


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
    scheduler.add_job(job_reminder_24h, IntervalTrigger(minutes=15), id="reminder_24h", replace_existing=True)
    scheduler.add_job(job_reminder_2h, IntervalTrigger(minutes=10), id="reminder_2h", replace_existing=True)
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
