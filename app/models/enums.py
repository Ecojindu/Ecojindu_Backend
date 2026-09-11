"""Domain enumerations.

Stored as short strings rather than native PG enums: adding a value later is
a code change, not a migration + table rewrite.
"""
from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    PASSENGER = "passenger"
    DRIVER = "driver"
    OPERATIONS = "operations"
    SUPER_ADMIN = "super_admin"


ADMIN_ROLES = {UserRole.OPERATIONS, UserRole.SUPER_ADMIN}


class DriverStatus(StrEnum):
    ACTIVE = "active"
    OFF_DUTY = "off_duty"
    SUSPENDED = "suspended"


class VehicleStatus(StrEnum):
    ACTIVE = "active"
    CHARGING = "charging"
    MAINTENANCE = "maintenance"
    RETIRED = "retired"


class ServiceType(StrEnum):
    """What kind of service a route sells.

    `airport` books individual seats on the fixed timetable. `charter` hires a
    whole vehicle on a bespoke departure and is priced per vehicle, so it never
    appears in seat availability. `rail` is published but not yet operating.
    """

    AIRPORT = "airport"
    RAIL = "rail"
    CHARTER = "charter"


#: Service types a passenger can actually book a seat on today.
SEAT_BOOKABLE_SERVICES = {ServiceType.AIRPORT}


class CharterStatus(StrEnum):
    REQUESTED = "requested"
    QUOTED = "quoted"
    CONFIRMED = "confirmed"
    ASSIGNED = "assigned"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DECLINED = "declined"


class PassengerSex(StrEnum):
    MALE = "male"
    FEMALE = "female"


class TripStatus(StrEnum):
    SCHEDULED = "scheduled"
    BOARDING = "boarding"
    DEPARTED = "departed"
    ARRIVED = "arrived"
    CANCELLED = "cancelled"


class BookingStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


#: Statuses that still occupy a seat on the vehicle.
SEAT_HOLDING_STATUSES = {
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.CONFIRMED,
    BookingStatus.CHECKED_IN,
    BookingStatus.COMPLETED,
}


class BookingSource(StrEnum):
    WEB = "web"
    WHATSAPP = "whatsapp"
    ADMIN = "admin"
    SUBSCRIPTION = "subscription"
    AGENT = "agent"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    ABANDONED = "abandoned"
    REFUNDED = "refunded"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    PENDING_PAYMENT = "pending_payment"
    EXHAUSTED = "exhausted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class NotificationChannel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class NotificationType(StrEnum):
    CONFIRMATION = "confirmation"
    REMINDER_24H = "reminder_24h"
    REMINDER_2H = "reminder_2h"
    SCHEDULE_CHANGE = "schedule_change"
    CANCELLATION = "cancellation"
    OTP = "otp"
    PASSWORD_RESET = "password_reset"
    SUBSCRIPTION_ACTIVATED = "subscription_activated"
    SUBSCRIPTION_EXPIRING = "subscription_expiring"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
