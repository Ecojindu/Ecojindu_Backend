"""Test fixtures.

Tests run against a real PostgreSQL database (`ecojindu_test` by default) because
the behaviour under test — `SELECT … FOR UPDATE`, conditional UPDATEs, unique-index
idempotency — is database behaviour, not Python behaviour. SQLite would prove nothing.

Set TEST_DATABASE_URL to point somewhere else. Create the database first:

    createdb ecojindu_test

The schema is built once per session through a *synchronous* engine so it never
touches an event loop; each test then gets its own async engine inside its own
loop, which keeps asyncpg connections and the running loop in agreement.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password
from app.core.timeutil import combine_lagos, now_utc, today_lagos
from app.models import Base
from app.models.enums import SubscriptionStatus, TripStatus, UserRole
from app.models.fleet import Vehicle
from app.models.route import Route
from app.models.subscription import Subscription, SubscriptionPlan
from app.models.trip import Trip
from app.models.user import User

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    settings.DATABASE_URL.rsplit("/", 1)[0] + "/ecojindu_test",
)
SYNC_TEST_URL = TEST_DATABASE_URL.replace("+asyncpg", "")


@pytest.fixture(scope="session", autouse=True)
def schema():
    """Build a clean schema once, without involving an event loop."""
    engine = create_engine(SYNC_TEST_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()
    yield


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=10)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncSession:
    async with session_factory() as session:
        yield session
        await session.rollback()


def _unique(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


@pytest_asyncio.fixture
async def route(db: AsyncSession) -> Route:
    row = Route(
        name="Umuahia → Sam Mbakwe Airport",
        code=_unique("T"),
        origin_terminal="Nnenna Otti Bus Terminal, Umuahia",
        destination="Sam Mbakwe Airport, Owerri",
        distance_km=72,
        duration_mins=90,
        base_fare_kobo=1_500_000,
    )
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture
async def vehicle(db: AsyncSession) -> Vehicle:
    row = Vehicle(name="Test EV", plate_no=_unique("TST-"), seat_capacity=14)
    db.add(row)
    await db.commit()
    return row


def tomorrow_at(hour: int) -> datetime:
    return combine_lagos(today_lagos() + timedelta(days=1), time(hour, 0))


@pytest_asyncio.fixture
async def trip(db: AsyncSession, route: Route, vehicle: Vehicle) -> Trip:
    """A 4-seat departure tomorrow morning — small enough to exhaust in tests."""
    departure = tomorrow_at(9)
    row = Trip(
        route_id=route.id,
        service_date=today_lagos() + timedelta(days=1),
        departure_datetime=departure,
        arrival_estimate=departure + timedelta(minutes=90),
        vehicle_id=vehicle.id,
        status=TripStatus.SCHEDULED,
        seats_total=4,
        seats_booked=0,
        fare_kobo=route.base_fare_kobo,
    )
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture
async def passenger(db: AsyncSession) -> User:
    row = User(
        full_name="Amaka Obi",
        phone=f"+23480{uuid.uuid4().int % 100_000_000:08d}",
        email=f"amaka-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("Passenger@2026"),
        role=UserRole.PASSENGER,
    )
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture
async def plan(db: AsyncSession) -> SubscriptionPlan:
    row = SubscriptionPlan(
        name="Tier 1",
        code=_unique("P"),
        price_kobo=20_000_000,
        ride_credits=12,
        validity_days=90,
    )
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture
async def subscription(db: AsyncSession, passenger: User, plan: SubscriptionPlan) -> Subscription:
    """Deliberately only 2 credits, so exhaustion is reachable in a test."""
    row = Subscription(
        user_id=passenger.id,
        plan_id=plan.id,
        credits_total=2,
        credits_used=0,
        starts_at=now_utc(),
        expires_at=now_utc() + timedelta(days=90),
        status=SubscriptionStatus.ACTIVE,
        amount_paid_kobo=plan.price_kobo,
    )
    db.add(row)
    await db.commit()
    return row
