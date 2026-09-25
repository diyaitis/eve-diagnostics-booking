import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.deps import CurrentUser, DbSession, Pagination, page
from app.models import BookingStatus
from app.schemas import BookingCreate, BookingOut, Page
from app.services import bookings

router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.post("", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
def create_booking(data: BookingCreate, user: CurrentUser, db: DbSession):
    """Book a test at a centre. The amount is taken from the centre's current price, not the client."""
    return bookings.create_booking(db, user, data)


@router.get("", response_model=Page[BookingOut])
def list_my_bookings(
    user: CurrentUser,
    db: DbSession,
    pagination: Pagination,
    status_filter: Annotated[BookingStatus | None, Query(alias="status")] = None,
):
    items, total = bookings.list_bookings(db, user, status_filter, pagination.limit, pagination.offset)
    return page(items, total, pagination)


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return bookings.get_owned_booking(db, user, booking_id)


@router.post("/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(booking_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return bookings.cancel_booking(db, user, booking_id)
