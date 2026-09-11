"""Import every model so Alembic autogenerate and relationship resolution see them."""
from app.db.base import Base
from app.models.audit import AuditLog
from app.models.booking import Booking, Ticket
from app.models.charter import CharterRequest
from app.models.enums import (
    ADMIN_ROLES,
    SEAT_BOOKABLE_SERVICES,
    SEAT_HOLDING_STATUSES,
    CharterStatus,
    BookingSource,
    BookingStatus,
    DriverStatus,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
    PassengerSex,
    PaymentStatus,
    ServiceType,
    SubscriptionStatus,
    TripStatus,
    UserRole,
    VehicleStatus,
)
from app.models.fleet import Vehicle
from app.models.notification import Notification, NotificationTemplate, WhatsAppSession
from app.models.payment import Payment, WebhookEvent
from app.models.route import Route, RouteStop
from app.models.subscription import Subscription, SubscriptionPlan
from app.models.trip import Trip, TripTemplate
from app.models.user import Driver, OtpCode, PasswordResetToken, User

__all__ = [
    "ADMIN_ROLES",
    "SEAT_BOOKABLE_SERVICES",
    "SEAT_HOLDING_STATUSES",
    "AuditLog",
    "Base",
    "Booking",
    "BookingSource",
    "BookingStatus",
    "CharterRequest",
    "CharterStatus",
    "Driver",
    "DriverStatus",
    "Notification",
    "NotificationChannel",
    "NotificationStatus",
    "NotificationTemplate",
    "NotificationType",
    "OtpCode",
    "PassengerSex",
    "PasswordResetToken",
    "Payment",
    "PaymentStatus",
    "Route",
    "RouteStop",
    "ServiceType",
    "Subscription",
    "SubscriptionPlan",
    "SubscriptionStatus",
    "Ticket",
    "Trip",
    "TripStatus",
    "TripTemplate",
    "User",
    "UserRole",
    "Vehicle",
    "VehicleStatus",
    "WebhookEvent",
    "WhatsAppSession",
]
