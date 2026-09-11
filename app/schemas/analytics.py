from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel


class RevenuePoint(BaseModel):
    period: str
    revenue_kobo: int
    bookings: int
    seats: int


class ChannelBreakdown(BaseModel):
    source: str
    bookings: int
    seats: int
    revenue_kobo: int
    share_pct: float


class RoutePerformance(BaseModel):
    route_id: uuid.UUID
    route_name: str
    trips: int
    seats_offered: int
    seats_sold: int
    occupancy_pct: float
    revenue_kobo: int


class OccupancySlot(BaseModel):
    time_slot: str
    trips: int
    seats_offered: int
    seats_sold: int
    occupancy_pct: float


class SubscriptionSales(BaseModel):
    plan_id: uuid.UUID
    plan_name: str
    sold: int
    revenue_kobo: int
    active: int


class DashboardSummary(BaseModel):
    date: date
    trips_today: int
    departures_remaining: int
    seats_offered_today: int
    seats_sold_today: int
    occupancy_today_pct: float
    revenue_today_kobo: int
    revenue_week_kobo: int
    revenue_month_kobo: int
    bookings_today: int
    bookings_week: int
    bookings_month: int
    active_subscriptions: int
    subscription_revenue_month_kobo: int
    cancellation_rate_pct: float
    channel_breakdown: list[ChannelBreakdown]


class TripAlert(BaseModel):
    trip_id: uuid.UUID
    severity: str
    kind: str
    message: str
    departure_datetime: str


class OverviewTrip(BaseModel):
    trip_id: uuid.UUID
    route_name: str
    departure_datetime: str
    status: str
    seats_total: int
    seats_booked: int
    occupancy_pct: float
    driver_name: str | None
    vehicle_name: str | None


class AnalyticsOverview(BaseModel):
    summary: DashboardSummary
    todays_trips: list[OverviewTrip]
    upcoming_departures: list[OverviewTrip]
    alerts: list[TripAlert]
