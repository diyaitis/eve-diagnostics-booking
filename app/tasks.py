"""Background jobs. Each one is safe to run twice, because a Celery task can be redelivered."""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import exists, select
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.database import SessionLocal
from app.logging_config import log_event
from app.models import Booking, BookingStatus, Payment, User, utcnow
from app.services import bookings
from app.worker import celery_app

log = logging.getLogger(__name__)

SUBJECTS = {
    BookingStatus.CONFIRMED: "Your booking is confirmed",
    BookingStatus.FAILED: "Your payment did not go through",
    BookingStatus.CANCELLED: "Your booking was cancelled",
}


@celery_app.task(
    name="app.tasks.send_booking_notification",
    autoretry_for=(OperationalError,),  # the database briefly unavailable: try again with a growing delay
    retry_backoff=True,
    retry_backoff_max=120,
    max_retries=5,
)
def send_booking_notification(booking_id: str, status: str) -> None:
    """Tells the patient about their booking's new status.

    Sending is simulated by a log line - a real system would call an email/SMS provider here. That
    is exactly the kind of slow, failure-prone call that should not sit inside an API request.
    """
    with SessionLocal() as db:
        booking = db.get(Booking, uuid.UUID(booking_id))
        user = db.get(User, booking.user_id) if booking else None
        if booking is None or user is None:
            log_event(log, logging.WARNING, "notification_skipped", booking_id=booking_id, reason="booking_not_found")
            return
        subject = SUBJECTS[BookingStatus(status)]
        log_event(
            log, logging.INFO, "notification_sent",
            booking_id=booking_id, to=user.email, subject=subject, status=status,
        )


@celery_app.task(name="app.tasks.expire_unpaid_bookings")
def expire_unpaid_bookings() -> int:
    """Cancels bookings that were made but never paid for, so they stop looking like live appointments.

    Only bookings with *no payment attempt at all* are touched: one with a payment in flight is
    waiting for a webhook and must be left to it. Returns how many were cancelled.
    """
    cutoff = utcnow() - timedelta(hours=get_settings().unpaid_booking_expiry_hours)
    cancelled = 0
    with SessionLocal() as db:
        stale_ids = db.scalars(
            select(Booking.id).where(
                Booking.status == BookingStatus.PENDING,
                Booking.created_at < cutoff,
                ~exists().where(Payment.booking_id == Booking.id),
            )
        ).all()
        for booking_id in stale_ids:
            # Lock the row and look again: it may just have been paid or cancelled by a request.
            booking = db.scalar(
                select(Booking).where(Booking.id == booking_id, Booking.status == BookingStatus.PENDING)
                .with_for_update(skip_locked=True)
            )
            if booking is None or booking.payments:
                db.rollback()
                continue
            bookings.transition(booking, BookingStatus.CANCELLED)
            db.commit()
            cancelled += 1
            log_event(log, logging.INFO, "booking_expired", booking_id=str(booking_id))
            bookings.notify(booking)
    return cancelled
