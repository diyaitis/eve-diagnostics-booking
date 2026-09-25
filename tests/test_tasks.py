import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import OperationalError

from app import tasks, worker
from app.database import SessionLocal
from app.models import Booking, BookingStatus, utcnow
from tests.helpers import event, future, pay, send_webhook


@pytest.fixture(autouse=True)
def capture_logs(caplog):
    caplog.set_level(logging.INFO)
    worker.reset_broker_state()
    yield
    worker.reset_broker_state()


def sent(caplog) -> list[dict]:
    return [r.fields for r in caplog.records if r.getMessage() == "notification_sent"]


def not_queued(caplog) -> list:
    return [r for r in caplog.records if r.getMessage() == "notification_not_queued"]


def new_booking(client, user_headers, catalogue):
    response = client.post(
        "/bookings",
        json={"centre_id": catalogue.centre_id, "test_id": catalogue.test_id, "appointment_at": future()},
        headers=user_headers,
    )
    return response.json()


def age(booking_id: str, hours: int) -> None:
    with SessionLocal() as db:
        db.get(Booking, uuid.UUID(booking_id)).created_at = utcnow() - timedelta(hours=hours)
        db.commit()


def status_of(client, headers, booking_id) -> str:
    return client.get(f"/bookings/{booking_id}", headers=headers).json()["status"]


# --- notifications ------------------------------------------------------------------------


def test_a_successful_payment_notifies_the_patient(client, user_headers, booking, caplog):
    pay(client, user_headers, booking["id"], "SUCCESS")

    (message,) = sent(caplog)
    assert message["to"] == "patient@example.com"
    assert message["status"] == "CONFIRMED"
    assert message["booking_id"] == booking["id"]
    assert message["subject"] == "Your booking is confirmed"


def test_a_declined_payment_notifies_the_patient(client, user_headers, booking, caplog):
    pay(client, user_headers, booking["id"], "FAILED")

    (message,) = sent(caplog)
    assert message["status"] == "FAILED"
    assert message["subject"] == "Your payment did not go through"


def test_cancelling_notifies_the_patient(client, user_headers, booking, caplog):
    client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    (message,) = sent(caplog)
    assert message["status"] == "CANCELLED"


def test_nothing_is_sent_while_the_payment_is_still_pending(client, user_headers, booking, caplog):
    pay(client, user_headers, booking["id"], "PENDING")

    assert sent(caplog) == []


def test_the_webhook_notifies_once_even_when_the_event_is_delivered_twice(
    client, user_headers, booking, caplog
):
    payment = pay(client, user_headers, booking["id"], "PENDING").json()
    payload = event("evt-1", payment["provider_reference"])

    send_webhook(client, payload)
    send_webhook(client, payload)  # duplicate delivery

    assert [m["status"] for m in sent(caplog)] == ["CONFIRMED"]


def test_a_second_event_with_the_same_outcome_sends_nothing_more(client, user_headers, booking, caplog):
    payment = pay(client, user_headers, booking["id"], "PENDING").json()

    send_webhook(client, event("evt-1", payment["provider_reference"]))
    send_webhook(client, event("evt-2", payment["provider_reference"]))  # a different event, same outcome

    assert [m["status"] for m in sent(caplog)] == ["CONFIRMED"]


def test_a_payment_that_changes_nothing_sends_nothing(client, user_headers, booking, caplog):
    client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)
    caplog.clear()
    payment_in_flight = client.post(  # a cancelled booking cannot be paid, so there is no payment at all
        "/payments/", json={"booking_id": booking["id"], "simulate_outcome": "SUCCESS"}, headers=user_headers
    )

    assert payment_in_flight.status_code == 409
    assert sent(caplog) == []


def test_queueing_failure_never_fails_the_request(client, user_headers, booking, caplog, monkeypatch):
    def broker_down(*args, **kwargs):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(tasks.send_booking_notification, "delay", broker_down)

    response = pay(client, user_headers, booking["id"], "SUCCESS")

    assert response.status_code == 201
    assert status_of(client, user_headers, booking["id"]) == "CONFIRMED"  # the real work was kept
    assert len(not_queued(caplog)) == 1


def test_a_dead_broker_is_not_retried_on_every_request(client, user_headers, catalogue, caplog, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(worker, "clock", lambda: now[0])
    attempts = []

    def broker_down(*args, **kwargs):
        attempts.append(1)
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(tasks.send_booking_notification, "delay", broker_down)

    for _ in range(4):
        booking = new_booking(client, user_headers, catalogue)
        assert client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers).status_code == 200

    assert len(attempts) == 1  # the other three were skipped without touching the broker
    assert len(not_queued(caplog)) == 4

    now[0] += worker.BROKER_RETRY_AFTER_SECONDS + 1
    booking = new_booking(client, user_headers, catalogue)
    client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)
    assert len(attempts) == 2  # tried again once the pause was over


def test_background_jobs_can_be_switched_off(client, user_headers, booking, caplog, monkeypatch):
    monkeypatch.setattr(worker, "background_jobs_enabled", lambda: False)

    response = pay(client, user_headers, booking["id"], "SUCCESS")

    assert response.status_code == 201
    assert sent(caplog) == [] and not_queued(caplog) == []


def test_a_notification_for_a_vanished_booking_is_skipped_not_crashed(caplog):
    tasks.send_booking_notification.delay(str(uuid.uuid4()), "CONFIRMED")

    assert sent(caplog) == []
    assert [r for r in caplog.records if r.getMessage() == "notification_skipped"]


def test_a_task_hitting_a_database_blip_is_retried(client, user_headers, booking, caplog, monkeypatch):
    real = tasks.SessionLocal
    attempts = []

    def flaky_session():
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("SELECT", {}, Exception("connection lost"))
        return real()

    monkeypatch.setattr(tasks, "SessionLocal", flaky_session)
    monkeypatch.setattr(tasks.send_booking_notification, "retry_backoff", False)  # do not wait in tests

    tasks.send_booking_notification.delay(booking["id"], "CONFIRMED")

    assert len(attempts) == 2
    assert len(sent(caplog)) == 1


# --- expiring unpaid bookings -------------------------------------------------------------


def test_old_unpaid_bookings_are_cancelled_and_the_patient_told(client, user_headers, catalogue, caplog):
    stale = new_booking(client, user_headers, catalogue)
    age(stale["id"], hours=25)

    assert tasks.expire_unpaid_bookings.delay().get() == 1

    assert status_of(client, user_headers, stale["id"]) == "CANCELLED"
    assert [m["status"] for m in sent(caplog)] == ["CANCELLED"]


def test_recent_bookings_are_left_alone(client, user_headers, catalogue):
    fresh = new_booking(client, user_headers, catalogue)
    age(fresh["id"], hours=23)

    assert tasks.expire_unpaid_bookings.delay().get() == 0
    assert status_of(client, user_headers, fresh["id"]) == "PENDING"


def test_a_booking_with_a_payment_in_flight_is_left_for_its_webhook(client, user_headers, catalogue):
    waiting = new_booking(client, user_headers, catalogue)
    payment = pay(client, user_headers, waiting["id"], "PENDING").json()
    age(waiting["id"], hours=48)

    assert tasks.expire_unpaid_bookings.delay().get() == 0

    send_webhook(client, event("evt-1", payment["provider_reference"]))
    assert status_of(client, user_headers, waiting["id"]) == "CONFIRMED"


def test_finished_bookings_are_never_touched(client, user_headers, catalogue):
    confirmed = new_booking(client, user_headers, catalogue)
    pay(client, user_headers, confirmed["id"], "SUCCESS")
    declined = new_booking(client, user_headers, catalogue)
    pay(client, user_headers, declined["id"], "FAILED")
    for booking in (confirmed, declined):
        age(booking["id"], hours=100)

    assert tasks.expire_unpaid_bookings.delay().get() == 0
    assert status_of(client, user_headers, confirmed["id"]) == "CONFIRMED"
    assert status_of(client, user_headers, declined["id"]) == "FAILED"


def test_running_the_expiry_twice_is_harmless(client, user_headers, catalogue):
    stale = new_booking(client, user_headers, catalogue)
    age(stale["id"], hours=30)

    results = [tasks.expire_unpaid_bookings.delay().get() for _ in range(2)]

    assert results == [1, 0]
    assert status_of(client, user_headers, stale["id"]) == "CANCELLED"


def test_expiry_is_scheduled_and_the_setup_is_sane():
    assert "expire-unpaid-bookings" in worker.celery_app.conf.beat_schedule
    assert worker.celery_app.conf.task_acks_late is True
    assert BookingStatus.CANCELLED in tasks.SUBJECTS
