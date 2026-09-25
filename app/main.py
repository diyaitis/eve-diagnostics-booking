import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import models  # noqa: F401  (registers the tables on Base.metadata)
from app.config import get_settings
from app.database import Base, engine
from app.errors import DomainError
from app.routers import auth, bookings, catalog, payments

DESCRIPTION = """
Diagnostic test bookings with simulated payments.

**Try it:** `POST /auth/signup`, then `Authorize` (top right) with the same email/password,
list `/centres`, `POST /bookings`, `POST /payments/`.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Simple bootstrap for the assignment. A real deployment would use Alembic migrations.
    Base.metadata.create_all(engine)
    yield


def create_app() -> FastAPI:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(title="EVE Diagnostics Booking API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def handle_domain_error(_request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(catalog.router)
    app.include_router(bookings.router)
    app.include_router(payments.router)
    return app


app = create_app()
