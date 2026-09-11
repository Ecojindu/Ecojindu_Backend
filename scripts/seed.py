"""Seed the database with the real Ecojindu Shuttle operating picture.

Idempotent — re-running updates the reference data in place rather than
duplicating it. Safe to run against a fresh database or an existing one.

    python -m scripts.seed
"""
from __future__ import annotations

import asyncio
import sys
from datetime import time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.core.timeutil import naira, now_utc  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.models.booking import Booking  # noqa: E402
from app.models.enums import (  # noqa: E402
    BookingSource,
    BookingStatus,
    DriverStatus,
    SubscriptionStatus,
    UserRole,
    VehicleStatus,
)
from app.models.fleet import Vehicle  # noqa: E402
from app.models.notification import NotificationTemplate  # noqa: E402
from app.models.route import Route, RouteStop  # noqa: E402
from app.models.subscription import Subscription, SubscriptionPlan  # noqa: E402
from app.models.trip import Trip, TripTemplate  # noqa: E402
from app.models.user import Driver, User  # noqa: E402
from app.services.bookings import create_booking, create_subscription_booking  # noqa: E402
from app.services.trips import generate_trips_from_templates  # noqa: E402

# ── Demo credentials (documented in the README) ──────────────
SUPER_ADMIN = ("Chidi Otti", "admin@ecojindu.ng", "+2348154471570", "Ecojindu@2026")
OPERATIONS = ("Ngozi Eze", "ops@ecojindu.ng", "+2348031234567", "Ecojindu@2026")
DRIVERS = [
    ("Emeka Nwosu", "emeka.driver@ecojindu.ng", "+2348062345678", "Driver@2026", "ABI-DRV-4471"),
    ("Uche Kalu", "uche.driver@ecojindu.ng", "+2348073456789", "Driver@2026", "ABI-DRV-5582"),
]
SUBSCRIBER = ("Amaka Obi", "amaka@example.com", "+2348090001122", "Passenger@2026")
GUEST = ("Ifeanyi Duru", "ifeanyi@example.com", "+2348123334455")


async def upsert_user(db: AsyncSession, *, full_name, email, phone, password, role) -> User:
    user = (await db.execute(select(User).where(User.phone == phone))).unique().scalar_one_or_none()
    if user is None:
        user = User(full_name=full_name, email=email, phone=phone, role=role)
        db.add(user)
    user.full_name = full_name
    user.email = email
    user.role = role
    user.password_hash = hash_password(password)
    user.is_active = True
    user.phone_verified = True
    user.email_verified = True
    await db.flush()
    return user


async def seed_vehicles(db: AsyncSession) -> list[Vehicle]:
    spec = [
        ("Ugwumba 1", "ABJ-114-EV", "Wuling EV Minibus", 14, 300),
        ("Ugwumba 2", "ABJ-115-EV", "Wuling EV Minibus", 14, 300),
    ]
    vehicles = []
    for name, plate, model, seats, rng in spec:
        v = (
            await db.execute(select(Vehicle).where(Vehicle.plate_no == plate))
        ).scalar_one_or_none()
        if v is None:
            v = Vehicle(plate_no=plate)
            db.add(v)
        v.name, v.model, v.seat_capacity, v.range_km = name, model, seats, rng
        v.status = VehicleStatus.ACTIVE
        vehicles.append(v)
    await db.flush()
    return vehicles


async def seed_routes(db: AsyncSession) -> dict[str, Route]:
    spec = [
        {
            "code": "UMU-OWA",
            "name": "Umuahia → Sam Mbakwe Airport",
            "origin_terminal": "Nnenna Otti Bus Terminal, Umuahia",
            "destination": "Sam Mbakwe International Cargo Airport, Owerri",
            "distance_km": 72.0,
            "duration_mins": 90,
            "base_fare_kobo": 1_500_000,  # ₦15,000
            "description": "Direct scheduled EV shuttle from Umuahia to the airport terminal.",
            "stops": [
                ("Nnenna Otti Bus Terminal, Umuahia", 5.5253, 7.4948, 0, True),
                ("Umuahia Tower Junction", 5.5321, 7.4867, 1, True),
                ("Umuahia–Owerri Road / Olokoro", 5.4712, 7.4321, 2, True),
                ("Naze Junction, Owerri", 5.5011, 7.0503, 3, True),
                ("Sam Mbakwe Airport Terminal", 5.4270, 7.2060, 4, False),
            ],
        },
        {
            "code": "OWA-UMU",
            "name": "Sam Mbakwe Airport → Umuahia",
            "origin_terminal": "Sam Mbakwe International Cargo Airport, Owerri",
            "destination": "Nnenna Otti Bus Terminal, Umuahia",
            "distance_km": 72.0,
            "duration_mins": 90,
            "base_fare_kobo": 1_500_000,
            "description": "Return leg meeting inbound flights at the arrivals hall.",
            "stops": [
                ("Sam Mbakwe Airport Arrivals", 5.4270, 7.2060, 0, True),
                ("Naze Junction, Owerri", 5.5011, 7.0503, 1, True),
                ("Umuahia Tower Junction", 5.5321, 7.4867, 2, False),
                ("Nnenna Otti Bus Terminal, Umuahia", 5.5253, 7.4948, 3, False),
            ],
        },
        {
            "code": "ABA-OWA",
            "name": "Aba → Sam Mbakwe Airport",
            "origin_terminal": "Aba Central Terminal",
            "destination": "Sam Mbakwe International Cargo Airport, Owerri",
            "distance_km": 65.0,
            "duration_mins": 85,
            "base_fare_kobo": 1_500_000,
            "description": "Scheduled EV shuttle connecting Aba's commercial hub to the airport.",
            "stops": [
                ("Aba Central Terminal", 5.1066, 7.3667, 0, True),
                ("Osisioma Junction, Aba", 5.1450, 7.3210, 1, True),
                ("Owerrinta", 5.2380, 7.2620, 2, True),
                ("Sam Mbakwe Airport Terminal", 5.4270, 7.2060, 3, False),
            ],
        },
        {
            "code": "OWA-ABA",
            "name": "Sam Mbakwe Airport → Aba",
            "origin_terminal": "Sam Mbakwe International Cargo Airport, Owerri",
            "destination": "Aba Central Terminal",
            "distance_km": 65.0,
            "duration_mins": 85,
            "base_fare_kobo": 1_500_000,
            "description": "Return leg into Aba.",
            "stops": [
                ("Sam Mbakwe Airport Arrivals", 5.4270, 7.2060, 0, True),
                ("Owerrinta", 5.2380, 7.2620, 1, True),
                ("Aba Central Terminal", 5.1066, 7.3667, 2, False),
            ],
        },
    ]

    routes: dict[str, Route] = {}
    for item in spec:
        stops = item.pop("stops")
        route = (
            await db.execute(select(Route).where(Route.code == item["code"]))
        ).unique().scalar_one_or_none()
        if route is None:
            route = Route(**item)
            db.add(route)
        else:
            for key, value in item.items():
                setattr(route, key, value)
        route.is_active = True
        await db.flush()

        # Query the stops explicitly — a freshly added Route has no loaded collection,
        # and touching `route.stops` would trigger a lazy load inside async code.
        current = (
            await db.execute(select(RouteStop).where(RouteStop.route_id == route.id))
        ).scalars().all()
        existing = {s.order: s for s in current}

        for name, lat, lng, order, pickup in stops:
            stop = existing.get(order)
            if stop is None:
                db.add(
                    RouteStop(
                        route_id=route.id, name=name, lat=lat, lng=lng, order=order,
                        pickup_allowed=pickup,
                    )
                )
            else:
                stop.name, stop.lat, stop.lng, stop.pickup_allowed = name, lat, lng, pickup
        routes[route.code] = route

    await db.flush()
    return routes


async def seed_plans(db: AsyncSession) -> list[SubscriptionPlan]:
    spec = [
        {
            "code": "TIER1",
            "name": "Ecojindu Commuter — Tier 1",
            "price_kobo": 20_000_000,  # ₦200,000
            "ride_credits": 12,
            "validity_days": 90,
            "description": "12 airport transfers over 3 months for regular business travellers.",
            "perks": "Priority boarding · Free rescheduling · Dedicated WhatsApp booking line",
            "sort_order": 1,
        },
        {
            "code": "TIER2",
            "name": "Ecojindu Corporate — Tier 2",
            "price_kobo": 100_000_000,  # ₦1,000,000
            "ride_credits": 50,
            "validity_days": 365,
            "description": "50 airport transfers over a full year — built for corporate accounts.",
            "perks": "Everything in Tier 1 · Named account manager · Monthly usage reporting · Transferable credits",
            "sort_order": 2,
        },
    ]
    plans = []
    for item in spec:
        plan = (
            await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.code == item["code"]))
        ).scalar_one_or_none()
        if plan is None:
            plan = SubscriptionPlan(**item)
            db.add(plan)
        else:
            for key, value in item.items():
                setattr(plan, key, value)
        plan.is_active = True
        plans.append(plan)
    await db.flush()
    return plans


async def seed_templates(
    db: AsyncSession, routes: dict[str, Route], vehicles: list[Vehicle], drivers: list[Driver]
) -> int:
    """The airline-style timetable: 06:00, 09:00, 12:00 and 15:00 daily each way."""
    slots = [time(6, 0), time(9, 0), time(12, 0), time(15, 0)]
    everyday = [1, 2, 3, 4, 5, 6, 7]

    assignment = {
        "UMU-OWA": (vehicles[0], drivers[0]),
        "OWA-UMU": (vehicles[0], drivers[0]),
        "ABA-OWA": (vehicles[1], drivers[1]),
        "OWA-ABA": (vehicles[1], drivers[1]),
    }

    created = 0
    for code, route in routes.items():
        vehicle, driver = assignment[code]
        for slot in slots:
            exists = (
                await db.execute(
                    select(TripTemplate).where(
                        TripTemplate.route_id == route.id, TripTemplate.departure_time == slot
                    )
                )
            ).unique().scalar_one_or_none()
            if exists:
                exists.vehicle_id, exists.driver_id, exists.is_active = vehicle.id, driver.id, True
                continue
            db.add(
                TripTemplate(
                    route_id=route.id,
                    departure_time=slot,
                    days_of_week=everyday,
                    vehicle_id=vehicle.id,
                    driver_id=driver.id,
                    is_active=True,
                )
            )
            created += 1
    await db.flush()
    return created


async def seed_notification_templates(db: AsyncSession) -> None:
    spec = [
        (
            "booking_confirmed_sms", "sms", None,
            "Ecojindu Shuttle CONFIRMED\nRef: {booking_ref}\n{origin} -> {destination}\n"
            "{date} {time}\nSeat(s): {seats}\nArrive 20 mins early.",
            "Sent immediately after a booking is paid for.",
        ),
        (
            "reminder_24h_sms", "sms", None,
            "Reminder: you travel tomorrow. Ref {booking_ref} | {origin} -> {destination} {date} {time}.",
            "Sent 24 hours before departure.",
        ),
        (
            "reminder_2h_sms", "sms", None,
            "Your Ecojindu shuttle departs in 2 hours. Ref {booking_ref} | {date} {time}. "
            "Please head to the terminal.",
            "Final call, 2 hours before departure.",
        ),
        (
            "schedule_change_sms", "sms", None,
            "Ecojindu Shuttle: {message}\nRef {booking_ref} | {origin} -> {destination} {date} {time}",
            "Broadcast when operations change or cancel a departure.",
        ),
        (
            "booking_confirmed_email", "email",
            "Your Ecojindu ticket · {booking_ref}",
            "Branded HTML template with the QR ticket embedded (app/templates/booking_confirmed.html).",
            "Subject line used for the confirmation email.",
        ),
    ]
    for key, channel, subject, body, description in spec:
        row = (
            await db.execute(select(NotificationTemplate).where(NotificationTemplate.key == key))
        ).scalar_one_or_none()
        if row is None:
            db.add(
                NotificationTemplate(
                    key=key, channel=channel, subject=subject, body=body, description=description
                )
            )
    await db.flush()


async def seed_demo_bookings(db: AsyncSession, subscriber: User) -> list[str]:
    """A handful of realistic bookings so dashboards aren't empty on day one."""
    if (await db.execute(select(Booking).limit(1))).unique().scalar_one_or_none():
        return []

    trips = list(
        (
            await db.execute(
                select(Trip).where(Trip.departure_datetime > now_utc()).order_by(Trip.departure_datetime).limit(6)
            )
        ).unique().scalars().all()
    )
    if not trips:
        return []

    refs: list[str] = []

    # A paid web booking.
    booking = await create_booking(
        db,
        trip_id=trips[0].id,
        passenger_name=GUEST[0],
        passenger_phone=GUEST[2],
        passenger_email=GUEST[1],
        seats=2,
        source=BookingSource.WEB,
    )
    booking.status = BookingStatus.CONFIRMED
    booking.confirmed_at = now_utc()
    booking.hold_expires_at = None
    refs.append(booking.booking_ref)

    # A WhatsApp booking still awaiting payment (shows the hold behaviour).
    pending = await create_booking(
        db,
        trip_id=trips[1].id,
        passenger_name="Blessing Ama",
        passenger_phone="+2348145556677",
        passenger_email="blessing@example.com",
        seats=1,
        source=BookingSource.WHATSAPP,
    )
    refs.append(pending.booking_ref)

    # A subscriber booking paid with ride credits.
    try:
        credit_booking = await create_subscription_booking(
            db, user=subscriber, trip_id=trips[2].id, seats=1
        )
        refs.append(credit_booking.booking_ref)
    except Exception as exc:  # noqa: BLE001 - demo data only
        print(f"  ! credit booking skipped: {exc}")

    await db.flush()
    return refs


async def main() -> None:
    print("\n\033[1mSeeding Ecojindu Shuttle\033[0m")
    print("─" * 58)

    async with session_scope() as db:
        super_admin = await upsert_user(
            db, full_name=SUPER_ADMIN[0], email=SUPER_ADMIN[1], phone=SUPER_ADMIN[2],
            password=SUPER_ADMIN[3], role=UserRole.SUPER_ADMIN,
        )
        ops = await upsert_user(
            db, full_name=OPERATIONS[0], email=OPERATIONS[1], phone=OPERATIONS[2],
            password=OPERATIONS[3], role=UserRole.OPERATIONS,
        )
        print(f"  users            super_admin={super_admin.email}  operations={ops.email}")

        vehicles = await seed_vehicles(db)
        print(f"  vehicles         {', '.join(f'{v.name} ({v.plate_no}, {v.seat_capacity} seats)' for v in vehicles)}")

        driver_rows: list[Driver] = []
        driver_names: list[str] = []
        for i, (name, email, phone, password, licence) in enumerate(DRIVERS):
            user = await upsert_user(
                db, full_name=name, email=email, phone=phone, password=password, role=UserRole.DRIVER
            )
            driver = (
                await db.execute(select(Driver).where(Driver.user_id == user.id))
            ).unique().scalar_one_or_none()
            if driver is None:
                driver = Driver(user_id=user.id, license_no=licence)
                db.add(driver)
            driver.license_no = licence
            driver.assigned_vehicle_id = vehicles[i % len(vehicles)].id
            driver.status = DriverStatus.ACTIVE
            await db.flush()
            driver_rows.append(driver)
            driver_names.append(user.full_name)
        print(f"  drivers          {', '.join(driver_names)}")

        routes = await seed_routes(db)
        stop_count = (
            await db.execute(select(func.count()).select_from(RouteStop))
        ).scalar_one()
        print(f"  routes           {len(routes)} routes, {stop_count} stops")

        plans = await seed_plans(db)
        for plan in plans:
            print(f"  plan             {plan.name}: {naira(plan.price_kobo)} · "
                  f"{plan.ride_credits} rides · {plan.validity_days} days")

        created_templates = await seed_templates(db, routes, vehicles, driver_rows)
        print(f"  timetable        {created_templates} new template(s) — 06:00, 09:00, 12:00, 15:00 daily each way")

        await seed_notification_templates(db)

        generated = await generate_trips_from_templates(db, days_ahead=14)
        print(f"  trips            {generated} departures generated for the next 14 days")

        subscriber = await upsert_user(
            db, full_name=SUBSCRIBER[0], email=SUBSCRIBER[1], phone=SUBSCRIBER[2],
            password=SUBSCRIBER[3], role=UserRole.PASSENGER,
        )
        sub = (
            await db.execute(select(Subscription).where(Subscription.user_id == subscriber.id))
        ).unique().scalar_one_or_none()
        if sub is None:
            tier1 = plans[0]
            sub = Subscription(
                user_id=subscriber.id,
                plan_id=tier1.id,
                credits_total=tier1.ride_credits,
                credits_used=0,
                starts_at=now_utc(),
                expires_at=now_utc() + timedelta(days=tier1.validity_days),
                status=SubscriptionStatus.ACTIVE,
                amount_paid_kobo=tier1.price_kobo,
            )
            db.add(sub)
            await db.flush()
        print(f"  subscriber       {subscriber.email} · {sub.credits_remaining} ride credits remaining")

        refs = await seed_demo_bookings(db, subscriber)
        if refs:
            print(f"  demo bookings    {', '.join(refs)}")

    print("─" * 58)
    print("\033[1mDone.\033[0m Demo logins:\n")
    print(f"  super admin   {SUPER_ADMIN[1]:<28} {SUPER_ADMIN[3]}")
    print(f"  operations    {OPERATIONS[1]:<28} {OPERATIONS[3]}")
    print(f"  driver        {DRIVERS[0][1]:<28} {DRIVERS[0][3]}")
    print(f"  driver        {DRIVERS[1][1]:<28} {DRIVERS[1][3]}")
    print(f"  subscriber    {SUBSCRIBER[1]:<28} {SUBSCRIBER[3]}\n")


if __name__ == "__main__":
    asyncio.run(main())
