"""Structured logging: one JSON object per line, each tagged with the id of the request it belongs to.

Log with ``log_event(logger, logging.INFO, "payment_created", payment_id=..., status=...)``: the event
name becomes the message and every keyword becomes a searchable field, instead of text to be parsed.
"""

import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

# Set per request by the middleware; also readable from the worker threads sync endpoints run in.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_record_factory_installed = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    logger.log(level, event, extra={"fields": {"event": event, **fields}})


def setup_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Configures the root logger. Safe to call more than once."""
    global _record_factory_installed
    if not _record_factory_installed:
        # A record factory (not a handler filter) so every handler - including test capture - sees the id.
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            record.request_id = request_id_var.get()
            return record

        logging.setLogRecordFactory(factory)
        _record_factory_installed = True

    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if log_format == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
