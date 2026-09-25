import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.errors import ConflictError, NotFoundError, UnprocessableError
from app.logging_config import log_event
from app.models import Booking, BookingStatus, CentreTest, User, utcnow
from app.schemas import BookingCreate
from app.services.pagination import paginate

log = logging.getLogger(__name__)

# The booking state machine. Anything not listed is not allowed; FAILED and CANCELLED are final.
#
#   PENDING ──payment succeeds──► CONFIRMED ──cancel──► CANCELLED
#      │  └──payment fails──► FAILED
#      └────cancel──► CANCELLED
ALLOWED_TRANSITIONS: dict[BookingStatus, set[BookingStatus]] = {
    BookingStatus.PENDING: {BookingStatus.CONFIRMED, BookingStatus.FAILED, BookingStatus.CANCELLED},
    BookingStatus.CONFIRMED: {BookingStatus.CANCELLED},
}


def transition(booking: Booking, new_status: BookingStatus) -> None:
    if new_status not in ALLOWED_TRANSITIONS.get(booking.status, set()):
        raise ConflictError(f"A {booking.status.value} booking cannot become {new_status.value}")
    log_event(log, logging.INFO, "booking_status_changed", booking_id=booking.id, from_status=booking.status.value, to_status=new_status.value)
    booking.status = new_status


def notify(booking: Booking) -> None:
    """Queues an email/SMS about the booking's current status. Call it only after the change is committed.

    Never raises: a broker outage must not fail a request whose database work already succeeded, and
    a missed notification is preferable to telling the client an error happened when it did not.
    """
    from app import tasks  # imported here because tasks itself uses this module
    from app import worker

    if not worker.background_jobs_enabled() or booking.status not in tasks.SUBJECTS:
        return
    if worker.broker_recently_failed():
        log_event(log, logging.WARNING, "notification_not_queued", booking_id=str(booking.id), error="broker_down")
        return
    try:
        tasks.send_booking_notification.delay(str(booking.id), booking.status.value)
    except Exception as exc:  # noqa: BLE001 - see docstring
        worker.mark_broker_failed()
        log_event(
            log, logging.WARNING, "notification_not_queued",
            booking_id=str(booking.id), error=type(exc).__name__,
        )


def create_booking(db: Session, user: User, data: BookingCreate) -> Booking:
    offering = db.get(CentreTest, (data.centre_id, data.test_id))
    if offering is None:
        raise NotFoundError("This centre does not offer that test")
    if data.appointment_at <= utcnow():
        raise UnprocessableError("appointment_at must be in the future")

    booking = Booking(
        user_id=user.id,
        centre_id=data.centre_id,
        test_id=data.test_id,
        appointment_at=data.appointment_at,
        amount=offering.price,  # snapshot: later price changes do not affect this booking
    )
    db.add(booking)
    db.commit()
    log_event(log, logging.INFO, "booking_created", booking_id=booking.id, user_id=user.id, amount=str(booking.amount))
    return booking


def get_owned_booking(db: Session, user: User, booking_id: uuid.UUID, *, for_update: bool = False) -> Booking:
    """Loads a booking that belongs to ``user``.

    Someone else's booking is reported as "not found" (not "forbidden") so that IDs can't be probed.
    ``for_update`` takes a row lock so concurrent payments / webhooks / cancellations line up.
    """
    stmt = select(Booking).where(Booking.id == booking_id, Booking.user_id == user.id)
    if for_update:
        stmt = stmt.with_for_update()
    booking = db.scalar(stmt)
    if booking is None:
        raise NotFoundError("Booking not found")
    return booking


def list_bookings(
    db: Session, user: User, status: BookingStatus | None, limit: int, offset: int
) -> tuple[list[Booking], int]:
    stmt = (
        select(Booking)
        .where(Booking.user_id == user.id)
        .options(joinedload(Booking.offering).joinedload(CentreTest.centre))
        .order_by(Booking.created_at.desc(), Booking.id)
    )
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    return paginate(db, stmt, limit, offset)


def cancel_booking(db: Session, user: User, booking_id: uuid.UUID) -> Booking:
    booking = get_owned_booking(db, user, booking_id, for_update=True)
    transition(booking, BookingStatus.CANCELLED)
    db.commit()
    notify(booking)
    return booking
