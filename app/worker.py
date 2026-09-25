"""The Celery application: work that should not make an API request wait.

Start a worker (with the periodic scheduler) using:

    celery -A app.worker worker --beat --loglevel=INFO

The broker is Redis (REDIS_URL). With CELERY_TASK_ALWAYS_EAGER=true tasks run inline in the calling
process instead, which is what the tests use.
"""

import time

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging

from app.config import get_settings
from app.logging_config import setup_logging

settings = get_settings()

celery_app = Celery("eve", broker=settings.redis_url or "memory://", include=["app.tasks"])
celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_ignore_result=True,  # nothing reads task results; skip the result backend entirely
    # A task is acknowledged only once it has finished, so a worker that dies mid-task does not lose
    # it. The price is that a task can run twice, so every task here is safe to repeat.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # The API publishes tasks from inside a request: give up quickly if the broker is unreachable.
    broker_transport_options={"socket_connect_timeout": 1, "socket_timeout": 1},
    task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.2},
    timezone="UTC",
    beat_schedule={
        "expire-unpaid-bookings": {"task": "app.tasks.expire_unpaid_bookings", "schedule": 15 * 60.0},
    },
)


# After a failed publish, stop trying for a while. Otherwise a broker whose hostname no longer resolves
# would add several seconds (the DNS timeout) to every request that wants to queue something.
BROKER_RETRY_AFTER_SECONDS = 30.0
_broker_down_until = 0.0
clock = time.monotonic


def broker_recently_failed() -> bool:
    return clock() < _broker_down_until


def mark_broker_failed() -> None:
    global _broker_down_until
    _broker_down_until = clock() + BROKER_RETRY_AFTER_SECONDS


def reset_broker_state() -> None:
    global _broker_down_until
    _broker_down_until = 0.0


def background_jobs_enabled() -> bool:
    """True if tasks can run somewhere: inline (eager) or on a worker reachable through Redis."""
    return settings.celery_task_always_eager or settings.redis_url is not None


@celery_setup_logging.connect
def _configure_logging(**_kwargs) -> None:
    # Connecting to this signal stops Celery installing its own log format, so the worker logs the
    # same JSON lines as the API.
    setup_logging(settings.log_level, settings.log_format)
