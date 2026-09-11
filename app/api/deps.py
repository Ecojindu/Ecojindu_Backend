"""Shared FastAPI dependencies: current user, role gates, service-key auth."""
# NOTE: no `from __future__ import annotations` here on purpose. FastAPI resolves a
# class-instance dependency's __call__ annotations without access to the module
# globals, so deferred (string) annotations like `Request` would never resolve.

import uuid
from typing import Annotated, Optional

import jwt
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AuthError, ForbiddenError
from app.core.security import constant_time_equals, decode_token
from app.db.session import get_db
from app.models.enums import ADMIN_ROLES, UserRole
from app.models.user import Driver, User
from sqlalchemy import select

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def _user_from_token(db: AsyncSession, token: str) -> User:
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Your session has expired. Please sign in again.", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid authentication token.") from exc

    if payload.get("type") != "access":
        raise AuthError("A refresh token cannot be used to access this endpoint.")

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthError("Invalid authentication token.") from exc

    user = await db.get(User, user_id)
    if user is None:
        raise AuthError("This account no longer exists.")
    if not user.is_active:
        raise ForbiddenError("This account has been deactivated.")
    return user


async def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthError("Please sign in to continue.")
    return await _user_from_token(db, credentials.credentials)


async def get_optional_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User | None:
    """For endpoints that work for guests but personalise for signed-in users."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await _user_from_token(db, credentials.credentials)
    except (AuthError, ForbiddenError):
        return None


class RequireRoles:
    """`Depends(RequireRoles(UserRole.OPERATIONS, UserRole.SUPER_ADMIN))`."""

    def __init__(self, *roles: str):
        self.roles = {str(r) for r in roles}

    async def __call__(self, user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in self.roles:
            raise ForbiddenError("You don't have permission to do that.")
        return user


require_admin = RequireRoles(UserRole.OPERATIONS, UserRole.SUPER_ADMIN)
require_super_admin = RequireRoles(UserRole.SUPER_ADMIN)
require_driver = RequireRoles(UserRole.DRIVER)
require_staff = RequireRoles(UserRole.OPERATIONS, UserRole.SUPER_ADMIN, UserRole.DRIVER)

CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
AdminUser = Annotated[User, Depends(require_admin)]
SuperAdminUser = Annotated[User, Depends(require_super_admin)]
DriverUser = Annotated[User, Depends(require_driver)]
StaffUser = Annotated[User, Depends(require_staff)]


async def get_driver_profile(db: DbSession, user: DriverUser) -> Driver:
    driver = (
        await db.execute(select(Driver).where(Driver.user_id == user.id))
    ).unique().scalar_one_or_none()
    if driver is None:
        raise ForbiddenError("No driver profile is linked to this account.")
    return driver


CurrentDriver = Annotated[Driver, Depends(get_driver_profile)]


async def require_service_key(
    x_service_key: Annotated[str | None, Header(alias="X-Service-Key")] = None,
) -> str:
    """Machine-to-machine auth used by the ecojindu-api gateway."""
    if not x_service_key or not constant_time_equals(x_service_key, settings.SERVICE_API_KEY):
        raise AuthError("Invalid or missing service key.", code="invalid_service_key")
    return x_service_key


ServiceKey = Annotated[str, Depends(require_service_key)]


async def service_or_admin(
    db: DbSession,
    x_service_key: Annotated[str | None, Header(alias="X-Service-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Optional[User]:
    """Accept either a valid service key or an admin JWT.

    Returns the acting admin, or None when the caller is the internal service.
    """
    if x_service_key and constant_time_equals(x_service_key, settings.SERVICE_API_KEY):
        return None

    if authorization and authorization.lower().startswith("bearer "):
        user = await _user_from_token(db, authorization.split(" ", 1)[1])
        if user.role in {str(r) for r in ADMIN_ROLES}:
            return user
        raise ForbiddenError("You don't have permission to do that.")

    raise AuthError("Service key or admin token required.")


ServiceOrAdmin = Annotated[Optional[User], Depends(service_or_admin)]
