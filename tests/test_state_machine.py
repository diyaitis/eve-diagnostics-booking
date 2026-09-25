import itertools

import pytest

from app.errors import ConflictError
from app.models import Booking, BookingStatus
from app.services.bookings import ALLOWED_TRANSITIONS, transition

S = BookingStatus

ALLOWED = [
    (S.PENDING, S.CONFIRMED),
    (S.PENDING, S.FAILED),
    (S.PENDING, S.CANCELLED),
    (S.CONFIRMED, S.CANCELLED),
]
FORBIDDEN = [pair for pair in itertools.product(S, S) if pair not in ALLOWED]


@pytest.mark.parametrize("current,new", ALLOWED)
def test_allowed_transitions(current, new):
    booking = Booking(status=current)

    transition(booking, new)

    assert booking.status == new


@pytest.mark.parametrize("current,new", FORBIDDEN)
def test_every_other_transition_is_refused_and_leaves_the_status_alone(current, new):
    booking = Booking(status=current)

    with pytest.raises(ConflictError):
        transition(booking, new)

    assert booking.status == current


def test_failed_and_cancelled_are_final():
    assert S.FAILED not in ALLOWED_TRANSITIONS
    assert S.CANCELLED not in ALLOWED_TRANSITIONS
