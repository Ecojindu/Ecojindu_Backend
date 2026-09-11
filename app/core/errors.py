"""Domain exceptions and the global handlers that render them."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import log_event, request_id_ctx

logger = logging.getLogger("ecojindu.errors")


class AppError(Exception):
    """Base class for every expected, user-facing failure."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class PaymentError(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "payment_error"


class SeatUnavailableError(ConflictError):
    code = "seats_unavailable"


def _envelope(code: str, message: str, details: dict | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id_ctx.get(),
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):  # noqa: ARG001
        log_event(
            logger,
            logging.WARNING,
            "domain error",
            code=exc.code,
            path=request.url.path,
            detail=exc.message,
        )
        return JSONResponse(
            _envelope(exc.code, exc.message, exc.details), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):  # noqa: ARG001
        fields = [
            {"field": ".".join(str(p) for p in e["loc"][1:]) or "body", "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            _envelope("validation_error", "Some fields need attention.", {"fields": fields}),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):  # noqa: ARG001
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "rate_limited"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(_envelope(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):  # noqa: ARG001
        log_event(logger, logging.ERROR, "integrity error", path=request.url.path, detail=str(exc.orig))
        return JSONResponse(
            _envelope("conflict", "That record conflicts with something that already exists."),
            status_code=status.HTTP_409_CONFLICT,
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db(request: Request, exc: SQLAlchemyError):
        logger.exception("database error on %s", request.url.path, exc_info=exc)
        return JSONResponse(
            _envelope("database_error", "A database error occurred. Please try again."),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error on %s", request.url.path, exc_info=exc)
        return JSONResponse(
            _envelope("internal_error", "Something went wrong on our side."),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
