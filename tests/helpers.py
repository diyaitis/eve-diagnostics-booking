import json
from datetime import datetime, timedelta, timezone

from app.security import sign_webhook


def future(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def login_headers(client, email: str, password: str) -> dict:
    response = client.post("/auth/login", data={"username": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def signup_and_login(client, email: str, password: str = "s3cretpass") -> dict:
    response = client.post("/auth/signup", json={"email": email, "password": password, "full_name": "Test User"})
    assert response.status_code == 201, response.text
    return login_headers(client, email, password)


def pay(client, headers, booking_id: str, outcome: str | None = "SUCCESS"):
    body = {"booking_id": booking_id}
    if outcome:
        body["simulate_outcome"] = outcome
    return client.post("/payments/", json=body, headers=headers)


def send_webhook(client, payload: dict, *, signature: str | None = "valid"):
    """Posts a webhook. signature='valid' signs it correctly; None sends no signature header."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "valid":
        headers["X-Webhook-Signature"] = sign_webhook(body)
    elif signature is not None:
        headers["X-Webhook-Signature"] = signature
    return client.post("/payments/webhook/", content=body, headers=headers)


def event(event_id: str, reference: str, status: str = "SUCCESS") -> dict:
    return {"event_id": event_id, "provider_reference": reference, "status": status}
