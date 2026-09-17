"""Application configuration — 12-factor, everything from the environment."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App ───────────────────────────────────────────────────
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True
    APP_NAME: str = "Ecojindu Shuttle Backend"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    # ── Database ──────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ecojindu"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # ── Auth ──────────────────────────────────────────────────
    JWT_SECRET: str = "dev-change-me-super-secret-jwt-key-0123456789"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── Tickets ───────────────────────────────────────────────
    TICKET_HMAC_SECRET: str = "dev-change-me-ticket-hmac-secret-9876543210"
    QR_STORAGE_DIR: str = "./storage/qr"
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # ── Service-to-service ────────────────────────────────────
    SERVICE_API_KEY: str = "dev-service-key-change-me"

    # ── Paystack ──────────────────────────────────────────────
    PAYSTACK_SECRET_KEY: str = "sk_test_placeholder"
    PAYSTACK_PUBLIC_KEY: str = "pk_test_placeholder"
    PAYSTACK_BASE_URL: str = "https://api.paystack.co"
    PAYSTACK_CALLBACK_URL: str = "http://localhost:3000/booking/callback"
    PAYSTACK_MOCK: bool = True

    # ── Email ─────────────────────────────────────────────────
    EMAIL_ENABLED: bool = True
    EMAIL_PROVIDER: Literal["console", "smtp"] = "console"
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    EMAIL_FROM: str = "jinduinc@gmail.com"
    EMAIL_FROM_NAME: str = "Ecojindu Shuttle"

    # ── SMS ───────────────────────────────────────────────────
    SMS_ENABLED: bool = True
    SMS_PROVIDER: Literal["console", "termii", "twilio"] = "console"
    TERMII_API_KEY: str = ""
    TERMII_SENDER_ID: str = "Ecojindu"
    TERMII_BASE_URL: str = "https://api.ng.termii.com"
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_SMS_FROM: str = "+15005550006"

    # ── Scheduler ─────────────────────────────────────────────
    SCHEDULER_ENABLED: bool = True
    TRIP_GENERATION_DAYS_AHEAD: int = 14
    SEAT_HOLD_MINUTES: int = 10

    # ── Upload & Go / ticket reading ──────────────────────────
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"
    CHECK_IN_BUFFER_HOURS: float = 2.5
    TICKET_READ_MAX_BYTES: int = 10 * 1024 * 1024
    TICKET_READ_RATE_LIMIT_PER_MINUTE: int = 10
    #: Comma-separated hours before trip departure to send reminders.
    REMINDER_OFFSETS_HOURS: str = "24,3,1"

    # ── Cloud Tasks (reminder delivery; falls back to APScheduler) ──
    CLOUD_TASKS_ENABLED: bool = False
    CLOUD_TASKS_PROJECT: str = ""
    CLOUD_TASKS_LOCATION: str = "us-central1"
    CLOUD_TASKS_QUEUE: str = "ecojindu-reminders"
    #: Public URL that receives task POSTs (e.g. https://api.example.com).
    CLOUD_TASKS_SERVICE_URL: str = ""

    # ── Flight status ─────────────────────────────────────────
    AVIATIONSTACK_API_KEY: str = ""

    # ── CORS ──────────────────────────────────────────────────
    # Comma-separated browser origins. Localhost alone is not enough for Cloud
    # Run — include every deployed web/admin hostname (project-number and
    # hash-style *.run.app URLs are distinct origins). Override via env in prod.
    CORS_ORIGINS: str = (
        "http://localhost:3000,http://localhost:3001,"
        "https://ecojindu-web-480235407496.us-central1.run.app,"
        "https://ecojindu-web-d7apfb4v6q-uc.a.run.app,"
        "https://ecojindu-admin-480235407496.us-central1.run.app,"
        "https://ecojindu-admin-d7apfb4v6q-uc.a.run.app,"
        "https://ecojindu.ng,https://www.ecojindu.ng,https://admin.ecojindu.ng"
    )

    # ── Rate limiting ─────────────────────────────────────────
    RATE_LIMIT_ENABLED: bool = True
    AUTH_RATE_LIMIT_PER_MINUTE: int = 10

    # ── Branding ──────────────────────────────────────────────
    COMPANY_NAME: str = "Ecojindu Shuttle"
    COMPANY_EMAIL: str = "jinduinc@gmail.com"
    COMPANY_PHONE: str = "+2348154471570"
    COMPANY_WHATSAPP: str = "+2348154471570"
    COMPANY_SOCIAL: str = "@ecojindu.ng"
    WEB_BASE_URL: str = "http://localhost:3000"

    @field_validator("DATABASE_URL")
    @classmethod
    def _force_async_driver(cls, v: str) -> str:
        """Accept a plain postgres:// URL (Cloud SQL / Heroku style) and upgrade it."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def sync_database_url(self) -> str:
        """Alembic + any sync tooling needs psycopg2."""
        return self.DATABASE_URL.replace("+asyncpg", "")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def reminder_offsets_hours(self) -> list[float]:
        """Parse REMINDER_OFFSETS_HOURS into a list of floats (hours before departure)."""
        offsets: list[float] = []
        for part in self.REMINDER_OFFSETS_HOURS.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                offsets.append(float(part))
            except ValueError:
                continue
        return offsets or [24.0, 3.0, 1.0]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
