"""Retrying work that failed for a reason likely to go away on its own."""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError

from app.logging_config import log_event

log = logging.getLogger(__name__)

T = TypeVar("T")

# OperationalError is what the database driver raises for deadlocks, serialization failures, lock
# timeouts and dropped connections - failures where trying again is reasonable. Anything else
# (a bug, a constraint violation) is not retried.
TRANSIENT_ERRORS = (OperationalError,)


def call_with_retries(
    func: Callable[[], T],
    *,
    on_retry: Callable[[], None],
    attempts: int = 3,
    base_delay: float = 0.05,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Calls ``func``; on a transient error runs ``on_retry`` (e.g. a rollback), backs off and tries again.

    The delay doubles each time (0.05s, 0.1s, ...). The last error is re-raised once ``attempts``
    are used up. Only safe for idempotent work - which the webhook handler is by design.
    """
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except TRANSIENT_ERRORS as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log_event(
                log, logging.WARNING, "transient_error_retrying",
                attempt=attempt, of=attempts, delay_seconds=delay, error=type(exc).__name__,
            )
            on_retry()
            (sleep or time.sleep)(delay)
    raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises
