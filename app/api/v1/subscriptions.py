"""Subscription purchase and passenger credit balance."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import desc, select

from app.api.deps import CurrentUser, DbSession, ServiceOrAdmin
from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.security import hash_password
from app.models.enums import SubscriptionStatus, UserRole
from app.models.subscription import Subscription, SubscriptionPlan
from app.models.user import User
from app.schemas.booking import PaymentInit
from app.schemas.subscription import (
    SubscriptionOut,
    SubscriptionPurchase,
    SubscriptionPurchaseResponse,
)
from app.services import payments as payment_service

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


def _to_out(sub: Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        id=sub.id,
        user_id=sub.user_id,
        plan_id=sub.plan_id,
        plan_name=sub.plan.name if sub.plan else None,
        subscriber_name=sub.user.full_name if sub.user else None,
        subscriber_phone=sub.user.phone if sub.user else None,
        credits_total=sub.credits_total,
        credits_used=sub.credits_used,
        credits_remaining=sub.credits_remaining,
        starts_at=sub.starts_at,
        expires_at=sub.expires_at,
        status=sub.status,
        amount_paid_kobo=sub.amount_paid_kobo,
        created_at=sub.created_at,
    )


@router.post(
    "/purchase",
    response_model=SubscriptionPurchaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Buy a subscription tier",
    description=(
        "Creates (or links) the subscriber's account and returns a Paystack payment "
        "link. Credits are only activated once payment settles."
    ),
)
async def purchase(payload: SubscriptionPurchase, db: DbSession) -> SubscriptionPurchaseResponse:
    plan = await db.get(SubscriptionPlan, payload.plan_id)
    if plan is None or not plan.is_active:
        raise NotFoundError("That subscription plan isn't available.")

    user = (
        await db.execute(select(User).where(User.phone == payload.phone))
    ).unique().scalar_one_or_none()
    if user is None:
        user = (
            await db.execute(select(User).where(User.email == payload.email.lower()))
        ).unique().scalar_one_or_none()

    if user is None:
        # Auto-create the account; the passenger sets a password via "forgot password".
        user = User(
            full_name=payload.full_name,
            phone=payload.phone,
            email=payload.email,
            role=UserRole.PASSENGER,
            password_hash=hash_password(uuid.uuid4().hex),
        )
        db.add(user)
        await db.flush()
    elif not user.email:
        user.email = payload.email

    subscription = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        credits_total=plan.ride_credits,
        credits_used=0,
        status=SubscriptionStatus.PENDING_PAYMENT,
    )
    db.add(subscription)
    await db.flush()
    await db.refresh(subscription)

    payment = await payment_service.initialize_subscription_payment(db, subscription, payload.email)
    await db.commit()
    await db.refresh(subscription)

    return SubscriptionPurchaseResponse(
        subscription=_to_out(subscription),
        payment=PaymentInit(
            reference=payment.paystack_reference,
            authorization_url=payment.authorization_url or "",
            access_code=payment.access_code or "",
            public_key=settings.PAYSTACK_PUBLIC_KEY,
            amount_kobo=payment.amount_kobo,
            email=payload.email,
        ),
        message="Complete payment to activate your ride credits.",
    )


@router.get("/mine", response_model=list[SubscriptionOut], summary="My subscriptions")
async def my_subscriptions(db: DbSession, user: CurrentUser) -> list[SubscriptionOut]:
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(desc(Subscription.created_at))
    )
    return [_to_out(s) for s in (await db.execute(stmt)).unique().scalars().all()]


@router.get(
    "/mine/active",
    response_model=SubscriptionOut | None,
    summary="My active plan and remaining ride credits",
)
async def my_active_subscription(db: DbSession, user: CurrentUser) -> SubscriptionOut | None:
    from app.services.bookings import active_subscription_for_user

    sub = await active_subscription_for_user(db, user.id)
    await db.commit()
    return _to_out(sub) if sub else None


@router.get(
    "/internal/by-phone/{phone}",
    response_model=SubscriptionOut | None,
    summary="Service-to-service subscriber lookup by phone",
)
async def subscription_by_phone(phone: str, db: DbSession, caller: ServiceOrAdmin) -> SubscriptionOut | None:
    from app.schemas.common import normalise_phone
    from app.services.bookings import active_subscription_for_user

    try:
        normalised = normalise_phone(phone)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    user = (
        await db.execute(select(User).where(User.phone == normalised))
    ).unique().scalar_one_or_none()
    if user is None:
        return None

    sub = await active_subscription_for_user(db, user.id)
    await db.commit()
    return _to_out(sub) if sub else None
