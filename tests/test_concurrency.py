"""Races that only a real database can exercise (row locks + unique constraints).

Run with:  TEST_DATABASE_URL=postgresql+psycopg://eve:eve@localhost:5432/eve_test pytest
On the default SQLite test database these are skipped.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal, engine
from app.main import app
from app.models import Payment, WebhookEvent
from tests.helpers import event, pay, send_webhook

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="needs real PostgreSQL row locks and constraints"
)

THREADS = 8


def run_in_parallel(work):
    """Runs work(client, index) in THREADS threads that all start at the same moment."""
    barrier = threading.Barrier(THREADS)

    def task(index):
        client = TestClient(app)
        barrier.wait()
        return work(client, index)

    with ThreadPoolExecutor(THREADS) as pool:
        return list(pool.map(task, range(THREADS)))


def count(model) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_the_same_webhook_delivered_many_times_at_once_is_applied_exactly_once(client, user_headers, booking):
    payment = pay(client, user_headers, booking["id"], "PENDING").json()
    payload = event("evt-race", payment["provider_reference"], "SUCCESS")

    results = run_in_parallel(lambda c, _i: send_webhook(c, payload))

    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    bodies = [r.json() for r in results]
    assert sum(1 for b in bodies if not b["duplicate"]) == 1
    assert all(b["result"] == "APPLIED" for b in bodies)
    assert count(WebhookEvent) == 1
    assert client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["status"] == "CONFIRMED"


def test_racing_payments_for_one_booking_create_exactly_one_payment(client, user_headers, booking):
    results = run_in_parallel(
        lambda c, _i: c.post(
            "/payments/",
            json={"booking_id": booking["id"], "simulate_outcome": "PENDING"},
            headers=user_headers,
        )
    )

    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * (THREADS - 1)
    assert count(Payment) == 1


def test_a_cancellation_racing_a_settlement_ends_in_a_consistent_state(client, user_headers, booking):
    payment = pay(client, user_headers, booking["id"], "PENDING").json()
    payload = event("evt-vs-cancel", payment["provider_reference"], "SUCCESS")

    def work(c, index):
        if index % 2 == 0:
            return c.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)
        return send_webhook(c, payload)

    run_in_parallel(work)

    # Whichever order the requests were served in, the outcome is the same: the payment settled
    # (the money did move) and the booking is cancelled, with the event recorded exactly once.
    final = client.get(f"/bookings/{booking['id']}", headers=user_headers).json()
    assert final["status"] == "CANCELLED"
    assert final["payments"][0]["status"] == "SUCCESS"
    assert count(WebhookEvent) == 1
