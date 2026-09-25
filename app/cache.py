"""Read-through cache for the catalogue (centres and tests), kept in Redis.

The catalogue is read far more often than it changes, so list/detail responses are cached as JSON.

Invalidation uses a version number: every cache key contains it, and any catalogue write bumps it
(after the write has committed). Old entries are then simply never read again and expire on their own,
which avoids hunting down every key a change might affect - and a reader that started before a write
cannot re-cache stale data under the new version.

Redis is an optimisation, never a dependency: with no REDIS_URL, or when Redis is down, requests are
served straight from the database. After a failure Redis is left alone for a few seconds so a dead
server costs one timeout, not one per request.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import redis

from app.config import get_settings
from app.logging_config import log_event

log = logging.getLogger(__name__)

VERSION_KEY = "catalogue:version"
RETRY_AFTER_FAILURE_SECONDS = 5.0

_client: "redis.Redis | None" = None
_client_ready = False
_down_until = 0.0
clock: Callable[[], float] = time.monotonic


def use_client(client: "redis.Redis | None") -> None:
    """Sets the Redis client explicitly (tests use an in-memory fake); None turns caching off."""
    global _client, _client_ready, _down_until
    _client, _client_ready, _down_until = client, True, 0.0


def _get_client() -> "redis.Redis | None":
    global _client, _client_ready
    if not _client_ready:
        url = get_settings().redis_url
        # Short timeouts: a slow cache must never make the API slower than having no cache.
        _client = redis.Redis.from_url(url, socket_connect_timeout=0.25, socket_timeout=0.25) if url else None
        _client_ready = True
    return _client


def _available() -> "redis.Redis | None":
    client = _get_client()
    if client is None or clock() < _down_until:
        return None
    return client


def _failed(action: str, exc: Exception) -> None:
    global _down_until
    _down_until = clock() + RETRY_AFTER_FAILURE_SECONDS
    log_event(log, logging.WARNING, "cache_unavailable", action=action, error=type(exc).__name__)


def cached_json(namespace: str, params: dict[str, Any], compute: Callable[[], Any]) -> tuple[Any, bool | None]:
    """Returns ``(value, hit)`` where ``hit`` is True/False, or None when the cache is not in use.

    ``compute`` must return something JSON-serialisable. Errors it raises propagate and nothing is cached.
    """
    client = _available()
    if client is None:
        return compute(), None

    key = None
    try:
        version = client.get(VERSION_KEY) or b"0"
        digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:32]
        key = f"catalogue:v{version.decode()}:{namespace}:{digest}"
        raw = client.get(key)
        if raw is not None:
            return json.loads(raw), True
    except (redis.RedisError, ValueError) as exc:
        _failed("read", exc)
        return compute(), None

    value = compute()
    try:
        client.set(key, json.dumps(value), ex=get_settings().catalogue_cache_ttl_seconds)
    except redis.RedisError as exc:
        _failed("write", exc)
    return value, False


def invalidate_catalogue() -> None:
    """Call after a catalogue change has been committed."""
    client = _get_client()
    if client is None:
        return
    try:
        client.incr(VERSION_KEY)
    except redis.RedisError as exc:
        # Cached entries stay until their TTL, which is why the TTL is kept short.
        _failed("invalidate", exc)
