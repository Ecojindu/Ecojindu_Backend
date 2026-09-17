"""Mounts every /v1 router."""
from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    bookings,
    catalog,
    charter,
    driver,
    flights,
    internal_jobs,
    payments,
    subscriptions,
    ticket_reading,
    tickets,
)

api_router = APIRouter(prefix="/v1")

api_router.include_router(auth.router)
api_router.include_router(catalog.router)
api_router.include_router(bookings.router)
api_router.include_router(payments.router)
api_router.include_router(tickets.router)
api_router.include_router(subscriptions.router)
api_router.include_router(charter.router)
api_router.include_router(admin.router)
api_router.include_router(driver.router)
api_router.include_router(ticket_reading.router)
api_router.include_router(flights.router)
api_router.include_router(internal_jobs.router)

__all__ = ["api_router"]
