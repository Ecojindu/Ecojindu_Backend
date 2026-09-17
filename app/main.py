"""Ecojindu Shuttle — core backend service."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import log_event, new_request_id, request_id_ctx, setup_logging
from app.db.session import engine
from app.jobs.scheduler import shutdown_scheduler, start_scheduler

setup_logging()
logger = logging.getLogger("ecojindu.app")

DESCRIPTION = """
The core service behind **Ecojindu Shuttle** — a scheduled, zero-emission EV airport
shuttle running the Umuahia/Aba ↔ Sam Mbakwe Airport (Owerri) corridor from the
Nnenna Otti Bus Terminal in Abia State.

It owns the database, business rules, payments, ticketing, notifications and the
timetable scheduler.

**Auth**

* Passengers, drivers and staff authenticate with a JWT bearer token (`/v1/auth/login`).
* The `ecojindu-api` gateway authenticates machine-to-machine with the `X-Service-Key`
  header on `/v1/**/internal/**` endpoints.

**Money** is always in **kobo** (₦1 = 100 kobo), matching Paystack.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
        from app.models import Base
        await conn.run_sync(Base.metadata.create_all)
    log_event(
        logger,
        logging.INFO,
        "backend starting",
        environment=settings.ENVIRONMENT,
        paystack_mock=settings.PAYSTACK_MOCK,
        sms_provider=settings.SMS_PROVIDER,
        email_provider=settings.EMAIL_PROVIDER,
    )
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()
        await engine.dispose()
        logger.info("backend stopped")


app = FastAPI(
    title=settings.APP_NAME,
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "Ecojindu Shuttle", "email": settings.COMPANY_EMAIL},
    license_info={"name": "Proprietary"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    # Custom domains under ecojindu.ng (apex is listed explicitly in CORS_ORIGINS).
    allow_origin_regex=r"https://([a-z0-9-]+\.)*ecojindu\.ng",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request id to every log line and response, and time the handler."""
    rid = request.headers.get("x-request-id") or new_request_id()
    token = request_id_ctx.set(rid)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = rid
    if not request.url.path.startswith(("/health", "/docs", "/openapi")):
        log_event(
            logger,
            logging.INFO,
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
    return response


register_exception_handlers(app)
app.include_router(api_router)


@app.get("/", tags=["Health"], summary="Service banner")
async def root() -> dict:
    return {
        "service": "ecojindu-backend",
        "version": "1.0.0",
        "tagline": "Bridging Cities, Powering Green Mobility",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["Health"], summary="Liveness and database check")
async def health() -> dict:
    db_ok = True
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
        logger.exception("health check: database unreachable")

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "environment": settings.ENVIRONMENT,
        "scheduler": settings.SCHEDULER_ENABLED,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.PORT, reload=settings.DEBUG)
