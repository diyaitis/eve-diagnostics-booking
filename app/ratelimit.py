"""Rate limiting for the endpoints that attract abuse: login (password guessing), signup (account
spam) and the public webhook URL.

It is a sliding-window limiter held in this process's memory, which is enough for one instance. Running
several instances would need a shared store such as Redis, otherwise each instance counts on its own.
The client is identified by the direct peer address; behind a reverse proxy, start uvicorn with
--proxy-headers so that address is the real client and not the proxy.
"""

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.logging_config import log_event

log = logging.getLogger(__name__)


class RateLimiter:
    """At most ``limit`` hits per ``window`` seconds for each key."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._checks = 0

    def check(self, key: str) -> float | None:
        """Records a hit. Returns None if it is allowed, or the seconds until a slot frees up.

        Refused hits are not recorded, so hammering a limited endpoint cannot extend the wait.
        """
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return self.window - (now - hits[0])
            hits.append(now)

            self._checks += 1
            if self._checks % 1000 == 0:
                self._forget_idle_keys(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _forget_idle_keys(self, now: float) -> None:
        # Keeps memory bounded when many different clients each visit once.
        idle = [key for key, hits in self._hits.items() if not hits or now - hits[-1] >= self.window]
        for key in idle:
            del self._hits[key]


_limiters: list[RateLimiter] = []


def rate_limit(limit: int, window_seconds: float = 60.0):
    """A FastAPI dependency that answers 429 (with Retry-After) once a client exceeds the limit."""
    limiter = RateLimiter(limit, window_seconds)
    _limiters.append(limiter)

    def dependency(request: Request) -> None:
        if not get_settings().rate_limit_enabled:
            return
        client = request.client.host if request.client else "unknown"
        retry_after = limiter.check(client)
        if retry_after is not None:
            log_event(log, logging.WARNING, "rate_limited", path=request.url.path, client=client)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests, please slow down",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )

    dependency.limiter = limiter  # exposed so tests can inspect and adjust it
    return dependency


signup_limit = rate_limit(10)
login_limit = rate_limit(10)
webhook_limit = rate_limit(120)


def reset_all_limiters() -> None:
    for limiter in _limiters:
        limiter.reset()
