# ── Ecojindu Shuttle — core backend ──────────────────────────
# Multi-stage: wheels are built once, the runtime image stays small.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Never run as root.
RUN useradd --create-home --uid 10001 ecojindu
WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY --chown=ecojindu:ecojindu . .

# QR images are written here; on Cloud Run this is the container's tmpfs, which is
# fine because `read_ticket_png` regenerates any missing file from the signed token.
RUN mkdir -p /app/storage/qr && chown -R ecojindu:ecojindu /app/storage

USER ecojindu
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

# Cloud Run injects $PORT; shell form so it expands.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 2 --proxy-headers
