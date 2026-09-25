import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import WebhookEvent
from app.services import payments
from app.services.retry import call_with_retries
from tests.helpers import event, pay, send_webhook


def transient_error():
    return OperationalError("UPDATE payments ...", {}, Exception("deadlock detected"))


def event_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(WebhookEvent))


# --- the retry helper ---------------------------------------------------------------------


def test_success_on_the_first_try_needs_no_retry():
    slept, rolled_back = [], []

    result = call_with_retries(lambda: "done", on_retry=lambda: rolled_back.append(1), sleep=slept.append)

    assert result == "done"
    assert slept == [] and rolled_back == []


def test_transient_errors_are_retried_with_doubling_backoff():
    attempts, slept, rolled_back = [], [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise transient_error()
        return "recovered"

    result = call_with_retries(flaky, on_retry=lambda: rolled_back.append(1), sleep=slept.append, base_delay=0.05)

    assert result == "recovered"
    assert len(attempts) == 3
    assert slept == [0.05, 0.1]
    assert len(rolled_back) == 2  # cleaned up before each retry


def test_gives_up_after_the_last_attempt_and_raises_the_error():
    attempts, rolled_back = [], []

    def always_failing():
        attempts.append(1)
        raise transient_error()

    with pytest.raises(OperationalError):
        call_with_retries(always_failing, on_retry=lambda: rolled_back.append(1), sleep=lambda _: None, attempts=3)

    assert len(attempts) == 3
    assert len(rolled_back) == 2  # not after the final failure: nothing left to retry


@pytest.mark.parametrize("error", [ValueError("bug"), IntegrityError("INSERT", {}, Exception("dup"))])
def test_other_errors_are_never_retried(error):
    attempts = []

    def failing():
        attempts.append(1)
        raise error

    with pytest.raises(type(error)):
        call_with_retries(failing, on_retry=lambda: None, sleep=lambda _: None)

    assert len(attempts) == 1


def test_each_retry_is_logged(caplog):
    caplog.set_level(logging.WARNING)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise transient_error()

    call_with_retries(flaky, on_retry=lambda: None, sleep=lambda _: None)

    retried = [r for r in caplog.records if r.getMessage() == "transient_error_retrying"]
    assert len(retried) == 1
    assert retried[0].fields["attempt"] == 1


# --- the webhook endpoint -----------------------------------------------------------------


@pytest.fixture
def pending_payment(client, user_headers, booking):
    return pay(client, user_headers, booking["id"], "PENDING").json()


def test_a_transient_failure_is_retried_and_the_event_is_still_applied_once(
    client, user_headers, booking, pending_payment, monkeypatch
):
    real = payments.process_webhook
    calls = []

    def flaky(db, event_in, payload):
        calls.append(1)
        if len(calls) <= 2:
            raise transient_error()
        return real(db, event_in, payload)

    monkeypatch.setattr(payments, "process_webhook", flaky)

    response = send_webhook(client, event("evt-1", pending_payment["provider_reference"]))

    assert response.status_code == 200
    assert response.json() == {"event_id": "evt-1", "result": "APPLIED", "duplicate": False}
    assert len(calls) == 3
    assert event_count() == 1
    assert client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["status"] == "CONFIRMED"


def test_a_failed_commit_is_rolled_back_and_the_whole_event_is_reprocessed(
    client, user_headers, booking, pending_payment, monkeypatch
):
    """The realistic case: the work is done and flushed, then the COMMIT itself fails."""
    real_commit = Session.commit
    commits = []

    def flaky_commit(self):
        commits.append(1)
        if len(commits) == 1:
            raise transient_error()
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", flaky_commit)

    response = send_webhook(client, event("evt-1", pending_payment["provider_reference"]))

    assert response.status_code == 200
    assert response.json()["result"] == "APPLIED"
    assert len(commits) == 2
    assert event_count() == 1  # the discarded first attempt left nothing behind
    monkeypatch.undo()
    assert client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["status"] == "CONFIRMED"


def test_when_the_database_stays_down_the_provider_is_told_to_retry_later(
    client, user_headers, booking, pending_payment, monkeypatch
):
    def always_failing(db, event_in, payload):
        raise transient_error()

    monkeypatch.setattr(payments, "process_webhook", always_failing)

    response = send_webhook(client, event("evt-1", pending_payment["provider_reference"]))

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert event_count() == 0  # nothing recorded, so the provider's retry will be processed normally
    monkeypatch.undo()
    assert client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["status"] == "PENDING"


def test_the_providers_own_retry_succeeds_once_the_database_is_back(
    client, user_headers, booking, pending_payment, monkeypatch
):
    payload = event("evt-1", pending_payment["provider_reference"])
    real = payments.process_webhook
    monkeypatch.setattr(payments, "process_webhook", lambda *a: (_ for _ in ()).throw(transient_error()))
    assert send_webhook(client, payload).status_code == 503

    monkeypatch.setattr(payments, "process_webhook", real)  # database recovered
    retry = send_webhook(client, payload)

    assert retry.status_code == 200 and retry.json()["duplicate"] is False
    assert client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["status"] == "CONFIRMED"


def test_bugs_are_not_masked_as_temporary_outages(client, pending_payment, monkeypatch):
    def broken(db, event_in, payload):
        raise ValueError("a real bug")

    monkeypatch.setattr(payments, "process_webhook", broken)

    with pytest.raises(ValueError):  # surfaces as a 500 rather than a misleading 503
        send_webhook(client, event("evt-1", pending_payment["provider_reference"]))
