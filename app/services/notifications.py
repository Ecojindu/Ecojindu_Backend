"""The notification engine.

Every send is recorded in `notifications` so operations can prove what went out
and retry what didn't. Provider failures are logged and swallowed — a booking is
never rolled back because an SMS gateway was down.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import log_event
from app.core.timeutil import fmt_date, fmt_datetime, fmt_time, naira, now_utc
from app.models.booking import Booking, Ticket
from app.models.enums import NotificationChannel, NotificationStatus, NotificationType
from app.models.notification import Notification
from app.models.route import Route
from app.models.subscription import Subscription, SubscriptionPlan
from app.models.trip import Trip
from app.models.user import User
from app.services.email import InlineImage, render_template, send_email
from app.services.sms import send_sms
from app.services.tickets import read_ticket_png

logger = logging.getLogger("ecojindu.notifications")


@dataclass(slots=True)
class TripView:
    """Flattened trip facts the templates and SMS copy both need."""

    route_name: str
    origin: str
    destination: str
    departure_label: str
    date_label: str
    time_label: str
    duration_mins: int


def build_trip_view(trip: Trip, route: Route) -> TripView:
    return TripView(
        route_name=route.name,
        origin=route.origin_terminal,
        destination=route.destination,
        departure_label=fmt_datetime(trip.departure_datetime),
        date_label=fmt_date(trip.departure_datetime),
        time_label=fmt_time(trip.departure_datetime),
        duration_mins=route.duration_mins,
    )


async def _record(
    db: AsyncSession,
    *,
    channel: str,
    ntype: str,
    recipient: str,
    status: str,
    booking_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    trip_id: uuid.UUID | None = None,
    subject: str | None = None,
    provider: str | None = None,
    provider_message_id: str | None = None,
    error: str | None = None,
    payload: dict | None = None,
) -> Notification:
    note = Notification(
        channel=channel,
        type=ntype,
        recipient=recipient,
        status=status,
        booking_id=booking_id,
        user_id=user_id,
        trip_id=trip_id,
        subject=subject,
        provider=provider,
        provider_message_id=provider_message_id,
        error=error,
        attempts=1,
        payload=payload,
        sent_at=now_utc() if status == NotificationStatus.SENT else None,
        created_at=now_utc(),
    )
    db.add(note)
    await db.flush()
    return note


def _trip_details_rows(booking: Booking, view: TripView) -> list[tuple[str, str]]:
    rows = [
        ("Passenger", booking.passenger_name),
        ("From", view.origin),
        ("To", view.destination),
        ("Departs", view.departure_label),
        ("Journey time", f"{view.duration_mins} minutes"),
        ("Seats", str(booking.seats)),
    ]
    if booking.seat_numbers:
        rows.append(("Seat numbers", ", ".join(booking.seat_numbers)))
    rows.append(
        ("Amount paid", "Ride credit (subscription)" if booking.subscription_id else naira(booking.amount_kobo))
    )
    return rows


# ── Booking confirmation ─────────────────────────────────────


async def send_booking_confirmation(
    db: AsyncSession, booking: Booking, trip: Trip, route: Route, ticket: Ticket | None
) -> None:
    view = build_trip_view(trip, route)
    user = await db.get(User, booking.user_id) if booking.user_id else None

    # ── Email with the QR embedded inline ──
    if booking.passenger_email and (user is None or user.notify_email):
        images: list[InlineImage] = []
        qr_cid = None
        if ticket:
            try:
                qr_cid = f"ejsqr-{booking.booking_ref.lower()}"
                images.append(
                    InlineImage(cid=qr_cid, content=read_ticket_png(ticket), filename=f"{booking.booking_ref}.png")
                )
            except Exception as exc:  # noqa: BLE001 - send the email even without the image
                log_event(logger, logging.WARNING, "could not attach QR", error=str(exc))
                qr_cid = None

        subject = f"Your Ecojindu ticket · {booking.booking_ref} · {view.time_label} {view.date_label}"
        html = render_template(
            "booking_confirmed.html",
            subject=subject,
            booking=booking,
            trip=view,
            details=_trip_details_rows(booking, view),
            qr_cid=qr_cid,
        )
        text = (
            f"Booking confirmed — {booking.booking_ref}\n"
            f"{view.route_name}\n{view.departure_label}\n"
            f"Seats: {booking.seats}  Amount: {naira(booking.amount_kobo)}\n"
            f"Manage: {settings.WEB_BASE_URL}/manage?ref={booking.booking_ref}"
        )
        result = await send_email(booking.passenger_email, subject, html, text, images)
        await _record(
            db,
            channel=NotificationChannel.EMAIL,
            ntype=NotificationType.CONFIRMATION,
            recipient=booking.passenger_email,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            booking_id=booking.id,
            user_id=booking.user_id,
            trip_id=trip.id,
            subject=subject,
            provider=result.provider,
            provider_message_id=result.message_id,
            error=result.error,
        )

    # ── SMS ──
    if booking.passenger_phone and (user is None or user.notify_sms):
        body = (
            f"Ecojindu Shuttle CONFIRMED\n"
            f"Ref: {booking.booking_ref}\n"
            f"{view.origin} -> {view.destination}\n"
            f"{view.date_label} {view.time_label}\n"
            f"Seat(s): {booking.seats}\n"
            f"Show your QR at the terminal. Arrive 20 mins early.\n"
            f"{settings.WEB_BASE_URL}/manage?ref={booking.booking_ref}"
        )
        result = await send_sms(booking.passenger_phone, body)
        await _record(
            db,
            channel=NotificationChannel.SMS,
            ntype=NotificationType.CONFIRMATION,
            recipient=booking.passenger_phone,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            booking_id=booking.id,
            user_id=booking.user_id,
            trip_id=trip.id,
            provider=result.provider,
            provider_message_id=result.message_id,
            error=result.error,
            payload={"body": body},
        )


# ── Reminders ────────────────────────────────────────────────


async def send_trip_reminder(
    db: AsyncSession, booking: Booking, trip: Trip, route: Route, window: str
) -> None:
    """`window` is e.g. '24h', '3h', '2h', or '1h'."""
    from app.services.reminders import notification_type_for_window

    view = build_trip_view(trip, route)
    ntype = notification_type_for_window(window)
    user = await db.get(User, booking.user_id) if booking.user_id else None

    ticket = (
        await db.execute(select(Ticket).where(Ticket.booking_id == booking.id))
    ).scalar_one_or_none()

    headlines = {
        "24h": "You travel tomorrow",
        "3h": "Departing in 3 hours",
        "2h": "Departing in 2 hours",
        "1h": "Departing in 1 hour",
    }
    sms_leads = {
        "24h": "Reminder: you travel tomorrow",
        "3h": "Your Ecojindu shuttle departs in 3 hours",
        "2h": "Your Ecojindu shuttle departs in 2 hours",
        "1h": "Your Ecojindu shuttle departs in 1 hour",
    }
    headline = headlines.get(window, f"Departing in {window}")
    sms_lead = sms_leads.get(window, f"Your Ecojindu shuttle departs in {window}")

    if booking.passenger_email and (user is None or user.notify_email):
        images: list[InlineImage] = []
        qr_cid = None
        if ticket:
            try:
                qr_cid = f"ejsqr-{booking.booking_ref.lower()}"
                images.append(
                    InlineImage(cid=qr_cid, content=read_ticket_png(ticket), filename=f"{booking.booking_ref}.png")
                )
            except Exception:  # noqa: BLE001
                qr_cid = None

        subject = f"{headline} · {view.time_label} to {view.destination} · {booking.booking_ref}"
        html = render_template(
            "trip_reminder.html",
            subject=subject,
            booking=booking,
            trip=view,
            window=window,
            details=_trip_details_rows(booking, view),
            qr_cid=qr_cid,
        )
        result = await send_email(booking.passenger_email, subject, html, None, images)
        await _record(
            db,
            channel=NotificationChannel.EMAIL,
            ntype=ntype,
            recipient=booking.passenger_email,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            booking_id=booking.id,
            user_id=booking.user_id,
            trip_id=trip.id,
            subject=subject,
            provider=result.provider,
            error=result.error,
        )

    if booking.passenger_phone and (user is None or user.notify_sms):
        body = (
            f"{sms_lead}.\n"
            f"Ref {booking.booking_ref} | {view.origin} -> {view.destination}\n"
            f"{view.date_label} {view.time_label}\n"
            f"Arrive 20 mins early with your QR ticket."
        )
        result = await send_sms(booking.passenger_phone, body)
        await _record(
            db,
            channel=NotificationChannel.SMS,
            ntype=ntype,
            recipient=booking.passenger_phone,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            booking_id=booking.id,
            user_id=booking.user_id,
            trip_id=trip.id,
            provider=result.provider,
            error=result.error,
            payload={"body": body},
        )


# ── Schedule changes & cancellations ─────────────────────────


async def send_schedule_change(
    db: AsyncSession,
    booking: Booking,
    trip: Trip,
    route: Route,
    change_message: str,
    *,
    cancelled: bool = False,
) -> None:
    view = build_trip_view(trip, route)
    ntype = NotificationType.CANCELLATION if cancelled else NotificationType.SCHEDULE_CHANGE

    if booking.passenger_email:
        subject = (
            f"Cancelled: {view.time_label} to {view.destination} · {booking.booking_ref}"
            if cancelled
            else f"Schedule update · {booking.booking_ref}"
        )
        html = render_template(
            "schedule_change.html",
            subject=subject,
            booking=booking,
            trip=view,
            change_message=change_message,
            cancelled=cancelled,
        )
        result = await send_email(booking.passenger_email, subject, html)
        await _record(
            db,
            channel=NotificationChannel.EMAIL,
            ntype=ntype,
            recipient=booking.passenger_email,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            booking_id=booking.id,
            user_id=booking.user_id,
            trip_id=trip.id,
            subject=subject,
            provider=result.provider,
            error=result.error,
        )

    if booking.passenger_phone:
        body = (
            f"Ecojindu Shuttle: {change_message}\n"
            f"Ref {booking.booking_ref} | {view.origin} -> {view.destination} {view.date_label} {view.time_label}\n"
            f"Help: {settings.COMPANY_PHONE}"
        )
        result = await send_sms(booking.passenger_phone, body)
        await _record(
            db,
            channel=NotificationChannel.SMS,
            ntype=ntype,
            recipient=booking.passenger_phone,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            booking_id=booking.id,
            user_id=booking.user_id,
            trip_id=trip.id,
            provider=result.provider,
            error=result.error,
            payload={"body": body},
        )


# ── Subscriptions ────────────────────────────────────────────


async def send_subscription_activated(
    db: AsyncSession, subscription: Subscription, plan: SubscriptionPlan, user: User
) -> None:
    details = [
        ("Plan", plan.name),
        ("Ride credits", str(subscription.credits_total)),
        ("Valid from", fmt_date(subscription.starts_at) if subscription.starts_at else "—"),
        ("Valid until", fmt_date(subscription.expires_at) if subscription.expires_at else "—"),
        ("Amount paid", naira(subscription.amount_paid_kobo)),
    ]
    if user.email:
        subject = f"{plan.name} activated · {subscription.credits_total} ride credits"
        html = render_template(
            "subscription_activated.html",
            subject=subject,
            user=user,
            plan=plan,
            subscription=subscription,
            details=details,
        )
        result = await send_email(user.email, subject, html)
        await _record(
            db,
            channel=NotificationChannel.EMAIL,
            ntype=NotificationType.SUBSCRIPTION_ACTIVATED,
            recipient=user.email,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            user_id=user.id,
            subject=subject,
            provider=result.provider,
            error=result.error,
        )

    if user.phone:
        body = (
            f"Ecojindu Shuttle: {plan.name} is active.\n"
            f"{subscription.credits_total} ride credits, valid until "
            f"{fmt_date(subscription.expires_at) if subscription.expires_at else 'further notice'}.\n"
            f"Book with zero payment at {settings.WEB_BASE_URL}/dashboard"
        )
        result = await send_sms(user.phone, body)
        await _record(
            db,
            channel=NotificationChannel.SMS,
            ntype=NotificationType.SUBSCRIPTION_ACTIVATED,
            recipient=user.phone,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            user_id=user.id,
            provider=result.provider,
            error=result.error,
            payload={"body": body},
        )


async def send_subscription_expiring(
    db: AsyncSession, subscription: Subscription, user: User, days_left: int
) -> None:
    message = (
        f"Your Ecojindu subscription expires in {days_left} day{'s' if days_left != 1 else ''} "
        f"with {subscription.credits_remaining} ride credit(s) remaining."
    )
    if user.email:
        subject = f"Your ride credits expire in {days_left} day{'s' if days_left != 1 else ''}"
        html = render_template(
            "simple_notice.html",
            subject=subject,
            heading="Use them before they go",
            body=message + " Renew or schedule your remaining trips from your dashboard.",
            action_url=f"{settings.WEB_BASE_URL}/dashboard",
            action_label="Open my dashboard",
        )
        result = await send_email(user.email, subject, html)
        await _record(
            db,
            channel=NotificationChannel.EMAIL,
            ntype=NotificationType.SUBSCRIPTION_EXPIRING,
            recipient=user.email,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            user_id=user.id,
            subject=subject,
            provider=result.provider,
            error=result.error,
        )
    if user.phone:
        result = await send_sms(user.phone, f"Ecojindu Shuttle: {message}")
        await _record(
            db,
            channel=NotificationChannel.SMS,
            ntype=NotificationType.SUBSCRIPTION_EXPIRING,
            recipient=user.phone,
            status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
            user_id=user.id,
            provider=result.provider,
            error=result.error,
        )


# ── Transactional one-offs ───────────────────────────────────


async def send_otp_sms(db: AsyncSession, phone: str, code: str, purpose: str) -> None:
    body = f"{code} is your Ecojindu Shuttle verification code. It expires in 10 minutes. Never share it."
    result = await send_sms(phone, body)
    await _record(
        db,
        channel=NotificationChannel.SMS,
        ntype=NotificationType.OTP,
        recipient=phone,
        status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
        provider=result.provider,
        error=result.error,
        payload={"purpose": purpose},
    )


async def send_password_reset_email(db: AsyncSession, user: User, token: str) -> None:
    if not user.email:
        return
    url = f"{settings.WEB_BASE_URL}/auth/reset?token={token}"
    subject = "Reset your Ecojindu Shuttle password"
    html = render_template(
        "simple_notice.html",
        subject=subject,
        heading="Reset your password",
        body=(
            f"Hi {user.full_name.split()[0]}, use the button below to set a new password. "
            "The link is valid for 60 minutes."
        ),
        action_url=url,
        action_label="Choose a new password",
        footnote="If you didn't request this, you can safely ignore this email — nothing has changed.",
    )
    result = await send_email(user.email, subject, html)
    await _record(
        db,
        channel=NotificationChannel.EMAIL,
        ntype=NotificationType.PASSWORD_RESET,
        recipient=user.email,
        status=NotificationStatus.SENT if result.success else NotificationStatus.FAILED,
        user_id=user.id,
        subject=subject,
        provider=result.provider,
        error=result.error,
    )
