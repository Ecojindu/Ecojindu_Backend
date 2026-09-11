"""Registration, login, refresh, OTP phone verification, password reset."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import jwt as pyjwt
from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.errors import AuthError, ConflictError, ValidationError
from app.core.ratelimit import RateLimiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_otp,
    hash_password,
    verify_password,
)
from app.core.timeutil import now_utc
from app.models.enums import UserRole
from app.models.user import OtpCode, PasswordResetToken, User
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    RequestOtp,
    ResetPasswordRequest,
    TokenPair,
    UpdateProfileRequest,
    UserOut,
    VerifyOtp,
)
from app.schemas.common import Message, normalise_phone
from app.services import notifications

router = APIRouter(prefix="/auth", tags=["Auth"])

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
RESET_TTL_MINUTES = 60


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _issue_tokens(user: User) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(str(user.id), user.role),
        refresh_token=create_refresh_token(str(user.id), user.role),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a passenger account",
    dependencies=[Depends(RateLimiter("register"))],
)
async def register(payload: RegisterRequest, db: DbSession) -> AuthResponse:
    existing = (
        await db.execute(select(User).where(User.phone == payload.phone))
    ).unique().scalar_one_or_none()

    if existing and existing.password_hash:
        raise ConflictError("An account already uses that phone number. Try signing in instead.")

    if payload.email:
        by_email = (
            await db.execute(select(User).where(User.email == payload.email))
        ).unique().scalar_one_or_none()
        if by_email and by_email.password_hash and (not existing or by_email.id != existing.id):
            raise ConflictError("An account already uses that email address.")

    if existing:
        # A guest booker claiming their account — keep their booking history.
        existing.full_name = payload.full_name
        existing.email = payload.email or existing.email
        existing.password_hash = hash_password(payload.password)
        user = existing
    else:
        user = User(
            full_name=payload.full_name,
            phone=payload.phone,
            email=payload.email,
            password_hash=hash_password(payload.password),
            role=UserRole.PASSENGER,
        )
        db.add(user)

    await db.flush()
    user.last_login_at = now_utc()
    await db.commit()
    await db.refresh(user)
    return AuthResponse(user=UserOut.model_validate(user), tokens=_issue_tokens(user))


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Sign in with email or phone",
    dependencies=[Depends(RateLimiter("login"))],
)
async def login(payload: LoginRequest, db: DbSession) -> AuthResponse:
    identifier = payload.identifier.strip()
    user = (
        await db.execute(select(User).where(User.email == identifier.lower()))
    ).unique().scalar_one_or_none()

    if user is None:
        try:
            phone = normalise_phone(identifier)
        except ValueError:
            phone = identifier
        user = (
            await db.execute(select(User).where(User.phone == phone))
        ).unique().scalar_one_or_none()

    if user is None or not verify_password(payload.password, user.password_hash or ""):
        raise AuthError("Those details don't match an account.")
    if not user.is_active:
        raise AuthError("This account has been deactivated. Please contact support.")

    user.last_login_at = now_utc()
    await db.commit()
    await db.refresh(user)
    return AuthResponse(user=UserOut.model_validate(user), tokens=_issue_tokens(user))


@router.post("/refresh", response_model=TokenPair, summary="Exchange a refresh token")
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token)
    except pyjwt.ExpiredSignatureError as exc:
        raise AuthError("Your session has expired. Please sign in again.") from exc
    except pyjwt.PyJWTError as exc:
        raise AuthError("Invalid refresh token.") from exc

    if claims.get("type") != "refresh":
        raise AuthError("That is not a refresh token.")

    user = await db.get(User, claims["sub"])
    if user is None or not user.is_active:
        raise AuthError("This account is no longer active.")
    return _issue_tokens(user)


@router.post(
    "/otp/request",
    response_model=Message,
    summary="Send a 6-digit SMS verification code",
    dependencies=[Depends(RateLimiter("otp", limit=5))],
)
async def request_otp(payload: RequestOtp, db: DbSession) -> Message:
    code = generate_otp()
    db.add(
        OtpCode(
            phone=payload.phone,
            code_hash=_sha256(code),
            purpose=payload.purpose,
            expires_at=now_utc() + timedelta(minutes=OTP_TTL_MINUTES),
            created_at=now_utc(),
        )
    )
    await db.flush()
    await notifications.send_otp_sms(db, payload.phone, code, payload.purpose)
    await db.commit()
    return Message(message=f"We sent a code to {payload.phone}. It expires in {OTP_TTL_MINUTES} minutes.")


@router.post("/otp/verify", response_model=Message, summary="Verify a phone number with an SMS code")
async def verify_otp(payload: VerifyOtp, db: DbSession) -> Message:
    stmt = (
        select(OtpCode)
        .where(
            OtpCode.phone == payload.phone,
            OtpCode.purpose == payload.purpose,
            OtpCode.consumed.is_(False),
        )
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    otp = (await db.execute(stmt)).scalar_one_or_none()

    if otp is None:
        raise ValidationError("Request a new code — we have none on file for that number.")
    if otp.expires_at < now_utc():
        raise ValidationError("That code has expired. Please request a new one.")
    if otp.attempts >= OTP_MAX_ATTEMPTS:
        raise ValidationError("Too many incorrect attempts. Please request a new code.")

    otp.attempts += 1
    if otp.code_hash != _sha256(payload.code.strip()):
        await db.commit()
        raise ValidationError("That code isn't right. Please check and try again.")

    otp.consumed = True
    user = (
        await db.execute(select(User).where(User.phone == payload.phone))
    ).unique().scalar_one_or_none()
    if user:
        user.phone_verified = True
    await db.commit()
    return Message(message="Phone number verified.")


@router.post(
    "/password/forgot",
    response_model=Message,
    summary="Email a password-reset link",
    dependencies=[Depends(RateLimiter("forgot", limit=5))],
)
async def forgot_password(payload: ForgotPasswordRequest, db: DbSession) -> Message:
    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).unique().scalar_one_or_none()

    # Always report success — never confirm whether an address is registered.
    if user:
        raw = secrets.token_urlsafe(32)
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=_sha256(raw),
                expires_at=now_utc() + timedelta(minutes=RESET_TTL_MINUTES),
                created_at=now_utc(),
            )
        )
        await db.flush()
        await notifications.send_password_reset_email(db, user, raw)
        await db.commit()

    return Message(message="If that address has an account, a reset link is on its way.")


@router.post("/password/reset", response_model=Message, summary="Set a new password using a reset token")
async def reset_password(payload: ResetPasswordRequest, db: DbSession) -> Message:
    token = (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == _sha256(payload.token))
        )
    ).scalar_one_or_none()

    if token is None or token.used or token.expires_at < now_utc():
        raise ValidationError("That reset link is invalid or has expired.")

    user = await db.get(User, token.user_id)
    if user is None:
        raise ValidationError("That reset link is invalid or has expired.")

    user.password_hash = hash_password(payload.new_password)
    token.used = True
    await db.commit()
    return Message(message="Your password has been updated. You can sign in now.")


@router.get("/me", response_model=UserOut, summary="The signed-in user")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.patch("/me", response_model=UserOut, summary="Update profile and notification preferences")
async def update_me(payload: UpdateProfileRequest, user: CurrentUser, db: DbSession) -> UserOut:
    data = payload.model_dump(exclude_unset=True)
    if data.get("email"):
        clash = (
            await db.execute(select(User).where(User.email == data["email"], User.id != user.id))
        ).unique().scalar_one_or_none()
        if clash:
            raise ConflictError("Another account already uses that email address.")
    for key, value in data.items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/me/password", response_model=Message, summary="Change your password")
async def change_password(payload: ChangePasswordRequest, user: CurrentUser, db: DbSession) -> Message:
    if not verify_password(payload.current_password, user.password_hash or ""):
        raise AuthError("Your current password isn't right.")
    user.password_hash = hash_password(payload.new_password)
    await db.commit()
    return Message(message="Password changed.")
