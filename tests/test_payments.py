import uuid

from app.config import get_settings
from app.deps import get_payment_provider
from app.main import app
from app.services.mock_provider import MockPaymentProvider
from tests.helpers import pay


def booking_status(client, headers, booking_id):
    return client.get(f"/bookings/{booking_id}", headers=headers).json()["status"]


def test_successful_payment_confirms_the_booking(client, user_headers, booking):
    response = pay(client, user_headers, booking["id"], "SUCCESS")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["amount"] == booking["amount"]
    assert body["provider_reference"].startswith("pay_")
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"


def test_failed_payment_fails_the_booking(client, user_headers, booking):
    response = pay(client, user_headers, booking["id"], "FAILED")

    assert response.status_code == 201  # a declined card is a business outcome, not an HTTP error
    assert response.json()["status"] == "FAILED"
    assert booking_status(client, user_headers, booking["id"]) == "FAILED"


def test_pending_payment_leaves_the_booking_pending(client, user_headers, booking):
    response = pay(client, user_headers, booking["id"], "PENDING")

    assert response.json()["status"] == "PENDING"
    assert booking_status(client, user_headers, booking["id"]) == "PENDING"


def test_booking_lists_its_payments(client, user_headers, booking):
    payment = pay(client, user_headers, booking["id"], "SUCCESS").json()

    payments = client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["payments"]

    assert [p["id"] for p in payments] == [payment["id"]]


def test_without_a_forced_outcome_the_provider_decides(client, user_headers, booking):
    app.dependency_overrides[get_payment_provider] = lambda: MockPaymentProvider(success_rate=1.0)
    try:
        response = pay(client, user_headers, booking["id"], outcome=None)
    finally:
        app.dependency_overrides.clear()

    assert response.json()["status"] == "SUCCESS"


def test_provider_can_decline(client, user_headers, booking):
    app.dependency_overrides[get_payment_provider] = lambda: MockPaymentProvider(success_rate=0.0)
    try:
        response = pay(client, user_headers, booking["id"], outcome=None)
    finally:
        app.dependency_overrides.clear()

    assert response.json()["status"] == "FAILED"


def test_mock_provider_outcome_follows_success_rate():
    assert MockPaymentProvider(1.0).charge("ref", 1) == "SUCCESS"
    assert MockPaymentProvider(0.0).charge("ref", 1) == "FAILED"


def test_cannot_pay_a_booking_twice(client, user_headers, booking):
    assert pay(client, user_headers, booking["id"], "SUCCESS").status_code == 201

    assert pay(client, user_headers, booking["id"], "SUCCESS").status_code == 409


def test_cannot_start_a_second_payment_while_one_is_pending(client, user_headers, booking):
    assert pay(client, user_headers, booking["id"], "PENDING").status_code == 201

    assert pay(client, user_headers, booking["id"], "SUCCESS").status_code == 409
    assert len(client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["payments"]) == 1


def test_failed_booking_cannot_be_paid_again(client, user_headers, booking):
    pay(client, user_headers, booking["id"], "FAILED")

    assert pay(client, user_headers, booking["id"], "SUCCESS").status_code == 409
    assert booking_status(client, user_headers, booking["id"]) == "FAILED"


def test_cancelled_booking_cannot_be_paid(client, user_headers, booking):
    client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    assert pay(client, user_headers, booking["id"], "SUCCESS").status_code == 409


def test_cannot_pay_someone_elses_booking(client, user_headers, other_headers, booking):
    response = pay(client, other_headers, booking["id"], "SUCCESS")

    assert response.status_code == 404
    assert booking_status(client, user_headers, booking["id"]) == "PENDING"


def test_paying_requires_authentication(client, booking):
    assert client.post("/payments/", json={"booking_id": booking["id"]}).status_code == 401


def test_invalid_payment_requests(client, user_headers):
    assert client.post("/payments/", json={}, headers=user_headers).status_code == 422
    assert client.post("/payments/", json={"booking_id": "nope"}, headers=user_headers).status_code == 422
    assert pay(client, user_headers, str(uuid.uuid4())).status_code == 404
    assert client.post(
        "/payments/", json={"booking_id": str(uuid.uuid4()), "simulate_outcome": "MAYBE"}, headers=user_headers
    ).status_code == 422


def test_forcing_an_outcome_can_be_switched_off(client, user_headers, booking, monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_simulated_outcome", False)

    assert pay(client, user_headers, booking["id"], "SUCCESS").status_code == 403
    assert booking_status(client, user_headers, booking["id"]) == "PENDING"


def test_a_payment_can_only_be_read_by_its_owner(client, user_headers, other_headers, booking):
    payment = pay(client, user_headers, booking["id"], "SUCCESS").json()

    assert client.get(f"/payments/{payment['id']}", headers=user_headers).json()["id"] == payment["id"]
    assert client.get(f"/payments/{payment['id']}", headers=other_headers).status_code == 404
    assert client.get(f"/payments/{payment['id']}").status_code == 401
    assert client.get(f"/payments/{uuid.uuid4()}", headers=user_headers).status_code == 404
