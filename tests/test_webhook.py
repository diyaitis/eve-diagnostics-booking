import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Booking, Payment, WebhookEvent
from tests.helpers import event, pay, send_webhook


def count(model) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model))


def booking_status(client, headers, booking_id):
    return client.get(f"/bookings/{booking_id}", headers=headers).json()["status"]


def payment_status(client, headers, payment_id):
    return client.get(f"/payments/{payment_id}", headers=headers).json()["status"]


@pytest.fixture
def pending_payment(client, user_headers, booking):
    """A payment the provider has accepted but not yet settled: awaiting its webhook."""
    response = pay(client, user_headers, booking["id"], "PENDING")
    assert response.status_code == 201
    return response.json()


# --- the happy paths ----------------------------------------------------------------------


def test_success_event_confirms_the_booking(client, user_headers, booking, pending_payment):
    response = send_webhook(client, event("evt-1", pending_payment["provider_reference"], "SUCCESS"))

    assert response.status_code == 200
    assert response.json() == {"event_id": "evt-1", "result": "APPLIED", "duplicate": False}
    assert payment_status(client, user_headers, pending_payment["id"]) == "SUCCESS"
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"


def test_failure_event_fails_the_booking(client, user_headers, booking, pending_payment):
    send_webhook(client, event("evt-1", pending_payment["provider_reference"], "FAILED"))

    assert payment_status(client, user_headers, pending_payment["id"]) == "FAILED"
    assert booking_status(client, user_headers, booking["id"]) == "FAILED"


# --- idempotency --------------------------------------------------------------------------


def test_replaying_the_same_event_changes_nothing(client, user_headers, booking, pending_payment):
    payload = event("evt-1", pending_payment["provider_reference"], "SUCCESS")

    first = send_webhook(client, payload).json()
    replays = [send_webhook(client, payload).json() for _ in range(3)]

    assert first["duplicate"] is False
    assert all(r == {"event_id": "evt-1", "result": "APPLIED", "duplicate": True} for r in replays)
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"
    assert (count(Booking), count(Payment), count(WebhookEvent)) == (1, 1, 1)


def test_replay_returns_the_original_result_even_if_the_payload_differs(client, user_headers, booking, pending_payment):
    reference = pending_payment["provider_reference"]
    send_webhook(client, event("evt-1", reference, "SUCCESS"))

    replay = send_webhook(client, event("evt-1", reference, "FAILED")).json()

    assert replay == {"event_id": "evt-1", "result": "APPLIED", "duplicate": True}
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"


def test_a_different_event_reporting_the_same_state_is_a_no_op(client, user_headers, booking, pending_payment):
    reference = pending_payment["provider_reference"]
    send_webhook(client, event("evt-1", reference, "SUCCESS"))

    again = send_webhook(client, event("evt-2", reference, "SUCCESS")).json()

    assert again == {"event_id": "evt-2", "result": "NO_CHANGE", "duplicate": False}
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"
    assert count(Payment) == 1


def test_a_late_failure_cannot_undo_a_successful_payment(client, user_headers, booking, pending_payment):
    reference = pending_payment["provider_reference"]
    send_webhook(client, event("evt-1", reference, "SUCCESS"))

    late = send_webhook(client, event("evt-2", reference, "FAILED")).json()

    assert late["result"] == "IGNORED"
    assert payment_status(client, user_headers, pending_payment["id"]) == "SUCCESS"
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"


def test_a_late_success_cannot_revive_a_failed_payment(client, user_headers, booking, pending_payment):
    reference = pending_payment["provider_reference"]
    send_webhook(client, event("evt-1", reference, "FAILED"))

    late = send_webhook(client, event("evt-2", reference, "SUCCESS")).json()

    assert late["result"] == "IGNORED"
    assert booking_status(client, user_headers, booking["id"]) == "FAILED"


def test_webhook_agrees_with_a_payment_already_settled_through_the_api(client, user_headers, booking):
    payment = pay(client, user_headers, booking["id"], "SUCCESS").json()
    reference = payment["provider_reference"]

    same = send_webhook(client, event("evt-1", reference, "SUCCESS")).json()
    conflicting = send_webhook(client, event("evt-2", reference, "FAILED")).json()

    assert same["result"] == "NO_CHANGE"
    assert conflicting["result"] == "IGNORED"
    assert booking_status(client, user_headers, booking["id"]) == "CONFIRMED"


def test_payment_settling_after_cancellation_is_recorded_but_the_booking_stays_cancelled(
    client, user_headers, booking, pending_payment
):
    client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    response = send_webhook(client, event("evt-1", pending_payment["provider_reference"], "SUCCESS"))

    assert response.json()["result"] == "APPLIED"
    assert payment_status(client, user_headers, pending_payment["id"]) == "SUCCESS"  # the money really moved
    assert booking_status(client, user_headers, booking["id"]) == "CANCELLED"  # but the booking is not revived


def test_the_event_id_is_the_idempotency_key_across_payments(client, user_headers, other_headers, catalogue, booking):
    """Documents the contract: an event_id is only ever applied once, whichever payment it names."""
    first = pay(client, user_headers, booking["id"], "PENDING").json()
    second_booking = client.post(
        "/bookings",
        json={"centre_id": catalogue.centre_id, "test_id": catalogue.test_id, "appointment_at": "2099-01-01T10:00:00Z"},
        headers=other_headers,
    ).json()
    second = pay(client, other_headers, second_booking["id"], "PENDING").json()

    send_webhook(client, event("shared-id", first["provider_reference"], "SUCCESS"))
    reused = send_webhook(client, event("shared-id", second["provider_reference"], "SUCCESS")).json()

    assert reused["duplicate"] is True
    assert payment_status(client, other_headers, second["id"]) == "PENDING"


# --- authenticity and bad input -----------------------------------------------------------


def test_events_without_a_valid_signature_are_rejected(client, user_headers, booking, pending_payment):
    payload = event("evt-1", pending_payment["provider_reference"], "SUCCESS")

    assert send_webhook(client, payload, signature=None).status_code == 401
    assert send_webhook(client, payload, signature="0" * 64).status_code == 401
    assert send_webhook(client, payload, signature="not-hex").status_code == 401
    assert payment_status(client, user_headers, pending_payment["id"]) == "PENDING"
    assert count(WebhookEvent) == 0


def test_a_signature_for_a_different_body_is_rejected(client, pending_payment):
    from app.security import sign_webhook

    genuine = b'{"event_id":"a","provider_reference":"x","status":"FAILED"}'
    tampered = f'{{"event_id":"evt-1","provider_reference":"{pending_payment["provider_reference"]}","status":"SUCCESS"}}'
    response = client.post(
        "/payments/webhook/",
        content=tampered.encode(),
        headers={"X-Webhook-Signature": sign_webhook(genuine), "Content-Type": "application/json"},
    )

    assert response.status_code == 401


def test_unknown_payment_reference_is_404_and_leaves_no_trace(client):
    response = send_webhook(client, event("evt-1", "pay_does_not_exist", "SUCCESS"))

    assert response.status_code == 404
    # nothing stored, so the provider can safely retry once the payment exists
    assert count(WebhookEvent) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"event_id": "e"},
        {"event_id": "", "provider_reference": "pay_1", "status": "SUCCESS"},
        {"event_id": "e", "provider_reference": "pay_1", "status": "PENDING"},
        {"event_id": "e", "provider_reference": "pay_1", "status": "REFUNDED"},
        {"event_id": "e", "provider_reference": "pay_1", "status": None},
        ["not", "an", "object"],
    ],
)
def test_malformed_events_are_rejected(client, payload):
    assert send_webhook(client, payload).status_code == 422
    assert count(WebhookEvent) == 0


def test_a_signed_body_that_is_not_json_is_rejected(client):
    from app.security import sign_webhook

    body = b"this is not json"
    response = client.post(
        "/payments/webhook/", content=body, headers={"X-Webhook-Signature": sign_webhook(body)}
    )

    assert response.status_code == 422
