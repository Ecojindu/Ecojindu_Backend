"""In-process sliding-window rate limiter for the auth endpoints.

Deliberately dependency-free. On Cloud Run each instance keeps its own
window, which is the right trade for brute-force slowing; swap the
`_Buckets` store for Redis if you later need a global limit.
"""
# NOTE: no `from __future__ import annotations` here on purpose. FastAPI resolves a
# class-instance dependency's __call__ annotations without access to the module
# globals, so deferred (string) annotations like `Request` would never resolve.

import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.config import settings
from app.core.errors import RateLimitError


class _Buckets:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                retry_after = int(q[0] + window_seconds - now) + 1
                return False, retry_after
            q.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_buckets = _Buckets()


def reset_rate_limits() -> None:
    """Used by the test-suite between cases."""
    _buckets.reset()


def _client_key(request: Request, scope: str) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    return f"{scope}:{ip}"


class RateLimiter:
    """FastAPI dependency: `Depends(RateLimiter("login"))`."""

    def __init__(self, scope: str, limit: int | None = None, window_seconds: int = 60):
        self.scope = scope
        self.limit = limit
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return
        limit = self.limit or settings.AUTH_RATE_LIMIT_PER_MINUTE
        allowed, retry_after = _buckets.hit(
            _client_key(request, self.scope), limit, self.window_seconds
        )
        if not allowed:
            raise RateLimitError(
                "Too many attempts. Please wait a moment and try again.",
                details={"retry_after_seconds": retry_after},
            )
