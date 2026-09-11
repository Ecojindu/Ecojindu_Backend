from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel, PhoneMixin


class RegisterRequest(PhoneMixin):
    full_name: str = Field(min_length=2, max_length=160)
    phone: str
    email: EmailStr | None = None
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    #: Email address or phone number — whichever the passenger remembers.
    identifier: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(ORMModel):
    id: uuid.UUID
    full_name: str
    email: str | None
    phone: str
    role: str
    is_active: bool
    phone_verified: bool
    email_verified: bool
    notify_email: bool
    notify_sms: bool
    notify_whatsapp: bool
    created_at: datetime


class AuthResponse(BaseModel):
    user: UserOut
    tokens: TokenPair


class RequestOtp(PhoneMixin):
    phone: str
    purpose: str = "phone_verify"


class VerifyOtp(PhoneMixin):
    phone: str
    code: str = Field(min_length=4, max_length=8)
    purpose: str = "phone_verify"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UpdateProfileRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    email: EmailStr | None = None
    notify_email: bool | None = None
    notify_sms: bool | None = None
    notify_whatsapp: bool | None = None
