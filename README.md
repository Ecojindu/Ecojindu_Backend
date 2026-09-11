# Ecojindu Shuttle — Core Backend

The heart of the Ecojindu Shuttle platform: a scheduled, zero-emission EV airport
shuttle running the **Umuahia / Aba ↔ Sam Mbakwe Airport (Owerri)** corridor from the
**Nnenna Otti Bus Terminal**, Abia State.

This service owns the database, the business rules, payments, ticketing, notifications
and the timetable scheduler. The three other services in the platform talk to it.

```
ecojindu-backend   ← you are here   FastAPI · owns Postgres · :8000
ecojindu-api                        FastAPI · WhatsApp + AI-agent gateway · :8001
ecojindu-web                        Next.js · customer site · :3000
ecojindu-admin                      Next.js · operations + driver portal · :3001
```

---

## What it does

| Area | Detail |
|---|---|
| **Auth** | JWT access + refresh, roles `passenger` / `driver` / `operations` / `super_admin`, SMS OTP phone verification, email password reset |
| **Timetable** | Airline-style `trip_templates` (06:00, 09:00, 12:00, 15:00 daily each way) materialised nightly into concrete bookable `trips` |
| **Seat holds** | Booking reserves seats for 15 minutes under a row lock; abandoned checkouts auto-release every minute |
| **Payments** | Paystack initialize / verify / signed webhooks, idempotent on retry |
| **Subscriptions** | Tier 1 ₦200,000 · 12 rides · 90 days; Tier 2 ₦1,000,000 · 50 rides · 365 days. Credit bookings are free at checkout and confirm instantly |
| **Ticketing** | HMAC-signed QR PNG per booking; scanner endpoint verifies the signature, the date and single use |
| **Notifications** | Branded HTML email (QR embedded inline) + SMS on confirmation, 24 h and 2 h reminders, schedule-change broadcasts |
| **Analytics** | Revenue trends, occupancy by route and time slot, channel mix, subscription sales, CSV export |

### Two invariants, enforced in the database

1. **Seats are never oversold.** Every change to `trips.seats_booked` happens while
   holding `SELECT … FOR UPDATE` on the trip row, so concurrent requests for the last
   seat serialise. See `app/services/trips.py::lock_trip`.
2. **A ride credit is never spent twice.** Credits are deducted by a conditional
   `UPDATE … WHERE credits_total - credits_used >= n`; a losing concurrent request
   updates zero rows and is rejected. See `app/services/bookings.py::deduct_credits_atomic`.

Both are covered by concurrency tests that spawn real parallel transactions.

---

## Setup

**Requirements:** Python 3.11+, PostgreSQL 14+.

```bash
cd ~/Desktop/ecojindu-backend

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit DATABASE_URL if needed
createdb ecojindu             # skip if it already exists

alembic upgrade head          # build the schema
python -m scripts.seed        # routes, timetable, fleet, staff, plans, demo bookings

uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000/docs** for the interactive OpenAPI reference.

### Demo logins

| Role | Email | Password |
|---|---|---|
| Super admin | `admin@ecojindu.ng` | `Ecojindu@2026` |
| Operations | `ops@ecojindu.ng` | `Ecojindu@2026` |
| Driver | `emeka.driver@ecojindu.ng` | `Driver@2026` |
| Driver | `uche.driver@ecojindu.ng` | `Driver@2026` |
| Subscriber | `amaka@example.com` | `Passenger@2026` |

The seed is idempotent — re-run `python -m scripts.seed` any time to top the data up.

---

## Environment

Every value lives in `.env` (see `.env.example` for the annotated full list).

### Runs with zero credentials

The defaults let the whole platform work locally with no third-party accounts:

| Var | Default | Effect |
|---|---|---|
| `PAYSTACK_MOCK` | `true` | Serves a local mock checkout page at `/v1/payments/mock-pay/<ref>`; clicking Pay settles the payment, confirms the booking, issues the QR and fires notifications |
| `EMAIL_PROVIDER` | `console` | Emails are logged instead of sent (subject, size, inline images) |
| `SMS_PROVIDER` | `console` | SMS bodies are logged verbatim |

Switch to live by setting real keys and `PAYSTACK_MOCK=false`, `EMAIL_PROVIDER=smtp`,
`SMS_PROVIDER=termii`.

### The ones that matter most

| Var | Notes |
|---|---|
| `DATABASE_URL` | Async driver. A plain `postgres://` URL is upgraded to `postgresql+asyncpg://` automatically |
| `JWT_SECRET` | Rotating it signs everyone out |
| `TICKET_HMAC_SECRET` | Rotating it **invalidates every already-issued QR ticket** |
| `SERVICE_API_KEY` | `ecojindu-api` sends this as `X-Service-Key` to reach `/v1/**/internal/**` |
| `SEAT_HOLD_MINUTES` | How long a pending-payment booking keeps its seats (default 15) |
| `CORS_ORIGINS` | Comma-separated. Must list both frontends |

Email works with any SMTP provider:

```
# Gmail      SMTP_HOST=smtp.gmail.com     SMTP_USERNAME=<you>     SMTP_PASSWORD=<app password>
# SendGrid   SMTP_HOST=smtp.sendgrid.net  SMTP_USERNAME=apikey    SMTP_PASSWORD=SG.xxxx
# Resend     SMTP_HOST=smtp.resend.com    SMTP_USERNAME=resend    SMTP_PASSWORD=re_xxxx
```

---

## Background jobs

APScheduler runs in-process (disable with `SCHEDULER_ENABLED=false`).

| Job | Cadence | Purpose |
|---|---|---|
| `release_holds` | every minute | Return seats from abandoned checkouts |
| `reminder_24h` | every 15 min | Email + SMS a day before travel |
| `reminder_2h` | every 10 min | Final call before departure |
| `generate_trips` | 00:20 daily | Materialise the next 14 days of the timetable |
| `subscription_hygiene` | 01:00 daily | Expire lapsed plans, warn 7 days out |

Every job is idempotent, so a missed or repeated fire is harmless. On Cloud Run with
more than one instance, prefer running the scheduler in a single dedicated instance
(`SCHEDULER_ENABLED=true` there, `false` elsewhere) or move these to Cloud Scheduler
hitting the equivalent admin endpoints.

---

## API shape

Everything is under `/v1`. Money is always in **kobo** (₦1 = 100 kobo), matching Paystack.

```
POST   /v1/auth/register | /login | /refresh
POST   /v1/auth/otp/request | /otp/verify
POST   /v1/auth/password/forgot | /password/reset

GET    /v1/routes                     routes + stops + fares
GET    /v1/trips?route_id&service_date&seats     live seat availability
GET    /v1/trips/availability?route_id&days      seats-per-day calendar
GET    /v1/plans                      subscription tiers

POST   /v1/bookings                   create + Paystack init (guest friendly)
POST   /v1/bookings/subscription      book with ride credits, zero payment
POST   /v1/bookings/lookup            find by reference + phone, no login
POST   /v1/bookings/{ref}/cancel | /resend-ticket

GET    /v1/payments/verify/{reference}
POST   /v1/payments/webhook/paystack  signature-verified, idempotent

GET    /v1/tickets/{ref}/qr.png       the boarding pass image
POST   /v1/tickets/validate           scanner check-in

POST   /v1/subscriptions/purchase
GET    /v1/subscriptions/mine/active  credit balance

GET    /v1/admin/analytics/overview | /revenue | /routes | /occupancy | /channels
GET    /v1/admin/analytics/export.csv
       /v1/admin/{routes,vehicles,drivers,templates,trips,bookings,plans,users}
GET    /v1/admin/trips/{id}/manifest

GET    /v1/driver/trips | /trips/today | /summary
GET    /v1/driver/trips/{id}/manifest
POST   /v1/driver/trips/{id}/status   Boarding → Departed → Arrived
```

### Service-to-service

`ecojindu-api` calls the `/internal/` endpoints with the `X-Service-Key` header:

```
POST /v1/bookings/internal                     create a booking
POST /v1/bookings/internal/subscription        credit booking by phone
GET  /v1/bookings/internal/{ref}               status lookup
POST /v1/bookings/internal/{ref}/cancel
POST /v1/bookings/internal/{ref}/resend-ticket
GET  /v1/subscriptions/internal/by-phone/{phone}
```

---

## Tests

```bash
createdb ecojindu_test
pytest
```

Tests run against real PostgreSQL, because what is under test *is* database behaviour.
`TEST_DATABASE_URL` overrides the target.

| File | Covers |
|---|---|
| `tests/test_seat_holds.py` | Race safety — 6 concurrent requests for 4 seats yield exactly 4 winners; hold expiry releases seats; confirmed bookings survive |
| `tests/test_payments_idempotency.py` | Duplicate and concurrent webhook deliveries produce one ticket; underpayment rejected; failed-then-success still settles |
| `tests/test_subscription_credits.py` | Concurrent credit spends never exceed the balance; exhaustion flips status; cancellation refunds the credit |
| `tests/test_ticket_hmac.py` | Tampered payloads fail the signature; wrong-date and unpaid tickets refused; second scan reports already-checked-in |

---

## Deploying to Cloud Run

```bash
PROJECT=your-gcp-project
REGION=africa-south1

gcloud builds submit --tag gcr.io/$PROJECT/ecojindu-backend

gcloud run deploy ecojindu-backend \
  --image gcr.io/$PROJECT/ecojindu-backend \
  --region $REGION \
  --platform managed \
  --allow-unauthenticated \
  --add-cloudsql-instances $PROJECT:$REGION:ecojindu-pg \
  --set-env-vars "ENVIRONMENT=production,DEBUG=false,LOG_JSON=true,PAYSTACK_MOCK=false" \
  --set-secrets "DATABASE_URL=ecojindu-db-url:latest,JWT_SECRET=ecojindu-jwt:latest,TICKET_HMAC_SECRET=ecojindu-ticket-hmac:latest,PAYSTACK_SECRET_KEY=paystack-secret:latest,SERVICE_API_KEY=ecojindu-service-key:latest" \
  --min-instances 1 --max-instances 10 --cpu 1 --memory 512Mi
```

Notes:

* Set `LOG_JSON=true` — the logger emits Cloud Logging's `severity` / `message` shape.
* Use Cloud SQL for Postgres; connect over the Unix socket
  (`postgresql+asyncpg://user:pass@/ecojindu?host=/cloudsql/<connection-name>`).
* Run `alembic upgrade head` from a Cloud Build step or a one-off Cloud Run job before
  routing traffic to a new revision.
* Keep `--min-instances 1` so the scheduler always has a live instance.
* Point the Paystack dashboard webhook at
  `https://<backend-url>/v1/payments/webhook/paystack`.
* QR PNGs are written to the container filesystem and regenerated on demand from the
  signed token, so a cold start loses nothing. Mount GCS with Cloud Storage FUSE only
  if you want them durable.

---

## Project layout

```
app/
  core/          config, logging, security (JWT + HMAC), errors, rate limiting, time
  db/            async engine, session factory, declarative base
  models/        SQLAlchemy 2.x models — the schema of record
  schemas/       Pydantic v2 request/response contracts
  services/      business logic: trips, bookings, payments, tickets, notifications,
                 analytics, Paystack, SMS + email providers
  api/v1/        routers: auth, catalog, bookings, payments, tickets,
                 subscriptions, admin, driver
  jobs/          APScheduler job definitions
  templates/     branded HTML email templates
alembic/         migrations
scripts/seed.py  idempotent demo/reference data
tests/           pytest against real PostgreSQL
```

---

Ecojindu Shuttle · Nnenna Otti Bus Terminal, Umuahia, Abia State
`jinduinc@gmail.com` · +234 815 447 1570 · @ecojindu.ng
*Bridging Cities, Powering Green Mobility.*
