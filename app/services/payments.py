import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.logging_config import log_event
from app.models import Booking, BookingStatus, Payment, PaymentStatus, User, WebhookEvent, WebhookResult
from app.schemas import PaymentCreate, WebhookIn
from app.services import bookings
from app.services.mock_provider import MockPaymentProvider

log = logging.getLogger(__name__)

LIVE_STATUSES = (PaymentStatus.PENDING, PaymentStatus.SUCCESS)


def _new_reference() -> str:
    return f"pay_{uuid.uuid4().hex[:24]}"


def apply_payment_result(booking: Booking, payment: Payment, outcome: PaymentStatus) -> WebhookResult:
    """The single place where a payment outcome is applied. Used by POST /payments/ and the webhook.

    A payment only ever moves PENDING -> SUCCESS or PENDING -> FAILED. A final payment never
    changes again, so a repeated, late or contradictory event cannot corrupt the booking.
    """
    if payment.status == outcome:
        return WebhookResult.NO_CHANGE
    if payment.status != PaymentStatus.PENDING:
        log_event(
            log, logging.WARNING, "payment_result_ignored",
            payment_id=payment.id, current=payment.status.value, received=outcome.value,
        )
        return WebhookResult.IGNORED

    payment.status = outcome
    if booking.status == BookingStatus.PENDING:
        bookings.transition(
            booking, BookingStatus.CONFIRMED if outcome == PaymentStatus.SUCCESS else BookingStatus.FAILED
        )
    elif outcome == PaymentStatus.SUCCESS:
        # Money was taken for a booking that is no longer payable (e.g. cancelled meanwhile).
        # The payment is recorded truthfully; the booking is left alone and flagged for a refund.
        log_event(
            log, logging.WARNING, "payment_for_closed_booking",
            payment_id=payment.id, booking_id=booking.id, booking_status=booking.status.value, needs_refund=True,
        )
    log_event(log, logging.INFO, "payment_result_applied", payment_id=payment.id, status=outcome.value)
    return WebhookResult.APPLIED


def create_payment(
    db: Session,
    user: User,
    data: PaymentCreate,
    provider: MockPaymentProvider,
    *,
    allow_simulated_outcome: bool,
) -> Payment:
    if data.simulate_outcome is not None and not allow_simulated_outcome:
        raise ForbiddenError("simulate_outcome is disabled in this environment")

    booking = bookings.get_owned_booking(db, user, data.booking_id, for_update=True)
    if booking.status != BookingStatus.PENDING:
        raise ConflictError(f"Booking is {booking.status.value}; only PENDING bookings can be paid")
    if any(p.status in LIVE_STATUSES for p in booking.payments):
        raise ConflictError("A payment for this booking is already in progress or completed")

    payment = Payment(booking_id=booking.id, amount=booking.amount, provider_reference=_new_reference())
    db.add(payment)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race: the partial unique index only allows one live payment per booking.
        db.rollback()
        raise ConflictError("A payment for this booking is already in progress or completed") from None

    outcome = provider.charge(payment.provider_reference, payment.amount, data.simulate_outcome)
    if outcome != PaymentStatus.PENDING:
        apply_payment_result(booking, payment, outcome)
    db.commit()
    log_event(log, logging.INFO, "payment_created", payment_id=payment.id, booking_id=booking.id, status=payment.status.value)
    return payment


def get_owned_payment(db: Session, user: User, payment_id: uuid.UUID) -> Payment:
    payment = db.scalar(
        select(Payment).join(Booking).where(Payment.id == payment_id, Booking.user_id == user.id)
    )
    if payment is None:
        raise NotFoundError("Payment not found")
    return payment


def _find_event(db: Session, event_id: str) -> WebhookEvent | None:
    return db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id))


def process_webhook(db: Session, event: WebhookIn, payload: dict) -> tuple[WebhookEvent, bool]:
    """Applies a provider event exactly once. Returns (stored event, was_duplicate).

    Idempotency comes from the unique constraint on ``webhook_events.event_id``: the event row is
    inserted in the same transaction that changes the payment/booking. If the insert collides,
    the event was already handled (even if a second copy arrived at the very same moment) and the
    stored result is returned without touching anything.
    """
    existing = _find_event(db, event.event_id)
    if existing is not None:
        log_event(log, logging.INFO, "webhook_duplicate", event_id=event.event_id, concurrent=False)
        return existing, True

    payment = db.scalar(select(Payment).where(Payment.provider_reference == event.provider_reference))
    if payment is None:
        raise NotFoundError("Unknown payment reference")

    # Lock order is always booking -> payment (same as create_payment / cancel) to avoid deadlocks.
    booking = db.scalar(select(Booking).where(Booking.id == payment.booking_id).with_for_update())
    db.refresh(payment, with_for_update=True)

    record = WebhookEvent(
        event_id=event.event_id, payment_id=payment.id, payload=payload, result=WebhookResult.NO_CHANGE
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = _find_event(db, event.event_id)
        if existing is None:  # pragma: no cover - the conflicting row was just committed by another request
            raise
        log_event(log, logging.INFO, "webhook_duplicate", event_id=event.event_id, concurrent=True)
        return existing, True

    record.result = apply_payment_result(booking, payment, event.status)
    db.commit()
    return record, False
