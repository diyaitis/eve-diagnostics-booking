import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.deps import CurrentUser, DbSession, PaymentProvider
from app.logging_config import log_event
from app.ratelimit import webhook_limit
from app.schemas import PaymentCreate, PaymentOut, WebhookIn, WebhookOut
from app.security import verify_webhook_signature
from app.services import payments
from app.services.retry import call_with_retries

log = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
def create_payment(data: PaymentCreate, user: CurrentUser, db: DbSession, provider: PaymentProvider):
    """Pay for one of your PENDING bookings through the simulated provider.

    A declined payment is a normal business outcome, not an HTTP error: the response is 201 with
    `"status": "FAILED"` and the booking becomes FAILED. If the provider answers PENDING, the final
    result arrives later on `POST /payments/webhook/`.
    """
    return payments.create_payment(
        db, user, data, provider, allow_simulated_outcome=get_settings().allow_simulated_outcome
    )


@router.get("/{payment_id}", response_model=PaymentOut)
def get_payment(payment_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return payments.get_owned_payment(db, user, payment_id)


async def _raw_body(request: Request) -> bytes:
    # The signature covers the exact bytes that were sent, so read them before any JSON parsing.
    return await request.body()


@router.post("/webhook/", response_model=WebhookOut, dependencies=[Depends(webhook_limit)])
def payment_webhook(
    body: Annotated[bytes, Depends(_raw_body)],
    db: DbSession,
    x_webhook_signature: Annotated[
        str | None, Header(description="Hex HMAC-SHA256 of the raw request body, keyed with WEBHOOK_SECRET")
    ] = None,
):
    """Receives payment-status updates from the payment provider. Safe to call repeatedly:
    an `event_id` is applied at most once, and later copies return the stored result."""
    if not verify_webhook_signature(body, x_webhook_signature):
        log_event(log, logging.WARNING, "webhook_rejected", reason="bad_signature")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")

    try:
        event = WebhookIn.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(
            exc.errors(include_url=False, include_context=False, include_input=False)
        ) from None

    payload = json.loads(body)
    try:
        # Safe to retry: an event_id is applied at most once, however many times this runs.
        record, duplicate = call_with_retries(
            lambda: payments.process_webhook(db, event, payload), on_retry=db.rollback
        )
    except OperationalError:
        # Still failing after the retries: tell the provider to try again later rather than lose the event.
        log_event(log, logging.ERROR, "webhook_unavailable", event_id=event.event_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Temporarily unable to process this event; please retry",
            headers={"Retry-After": "5"},
        ) from None
    return WebhookOut(event_id=record.event_id, result=record.result, duplicate=duplicate)
