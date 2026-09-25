import logging
import re
import time
import uuid

from fastapi import FastAPI, Request

from app.logging_config import log_event, request_id_var

access_log = logging.getLogger("app.access")

# A caller-supplied X-Request-ID is only trusted if it is short and plain, so it can't be used to
# smuggle newlines or huge strings into the logs.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def install_request_logging(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _VALID_REQUEST_ID.match(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500  # what an unhandled exception ends up as
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            log_event(
                access_log,
                logging.INFO,
                "request",
                method=request.method,
                path=request.url.path,
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            request_id_var.reset(token)
