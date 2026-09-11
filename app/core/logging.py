"""Structured logging — plain text locally, single-line JSON in the cloud."""
from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar

from app.core.config import settings

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class JsonFormatter(logging.Formatter):
    """Cloud Logging reads `severity` and `message` straight off the JSON payload."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": request_id_ctx.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rid = request_id_ctx.get()
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{rid}] {record.name}: {record.getMessage()}"
        extra = getattr(record, "extra_fields", {})
        if extra:
            base += "  " + " ".join(f"{k}={v}" for k, v in extra.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.LOG_JSON else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL.upper())

    # Quiet the noisy ones.
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, **fields) -> None:
    """Emit a log line with structured key/value fields attached."""
    logger.log(level, message, extra={"extra_fields": fields})
