from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings
from app.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    sign_webhook,
    verify_password,
    verify_webhook_signature,
)

SIGNUP = {"email": "Patient@Example.com", "password": "s3cretpass", "full_name": "  Asha Rao  "}


def test_signup_creates_user_without_exposing_password(client):
    response = client.post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "patient@example.com"  # normalised to lower case
    assert body["full_name"] == "Asha Rao"  # whitespace trimmed
    assert body["is_admin"] is False
    assert "password" not in body and "hashed_password" not in body


def test_signup_cannot_make_an_admin(client):
    response = client.post("/auth/signup", json={**SIGNUP, "is_admin": True})

    assert response.status_code == 201
    assert response.json()["is_admin"] is False


def test_duplicate_email_is_rejected_regardless_of_case(client):
    client.post("/auth/signup", json=SIGNUP)

    response = client.post("/auth/signup", json={**SIGNUP, "email": "PATIENT@example.com"})

    assert response.status_code == 409


def test_signup_validates_input(client):
    assert client.post("/auth/signup", json={**SIGNUP, "email": "not-an-email"}).status_code == 422
    assert client.post("/auth/signup", json={**SIGNUP, "password": "short"}).status_code == 422
    assert client.post("/auth/signup", json={**SIGNUP, "password": "x" * 73}).status_code == 422
    assert client.post("/auth/signup", json={**SIGNUP, "full_name": "   "}).status_code == 422
    assert client.post("/auth/signup", json={"email": "a@b.co"}).status_code == 422


def test_login_returns_a_working_bearer_token(client):
    client.post("/auth/signup", json=SIGNUP)

    response = client.post("/auth/login", data={"username": "patient@example.com", "password": "s3cretpass"})

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    token = response.json()["access_token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "patient@example.com"


def test_login_failures_do_not_reveal_whether_the_email_exists(client):
    client.post("/auth/signup", json=SIGNUP)

    wrong_password = client.post("/auth/login", data={"username": "patient@example.com", "password": "wrongpass1"})
    unknown_user = client.post("/auth/login", data={"username": "nobody@example.com", "password": "s3cretpass"})

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_protected_endpoint_rejects_missing_malformed_and_tampered_tokens(client, user_headers):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401

    good = user_headers["Authorization"].split()[1]
    tampered = good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401


def test_expired_token_is_rejected(client, user_headers):
    settings = get_settings()
    me = client.get("/auth/me", headers=user_headers).json()
    expired = jwt.encode(
        {"sub": me["id"], "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    assert client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_token_signed_with_another_secret_is_rejected(client, user_headers):
    me = client.get("/auth/me", headers=user_headers).json()
    forged = jwt.encode(
        {"sub": me["id"], "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, "a-different-secret-that-is-also-32-bytes!", algorithm="HS256"
    )

    assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_token_for_a_deleted_user_is_rejected(client):
    import uuid

    ghost = create_access_token(uuid.uuid4())

    assert client.get("/auth/me", headers={"Authorization": f"Bearer {ghost}"}).status_code == 401


# --- security helpers -------------------------------------------------------------------


def test_password_hashing_round_trip():
    hashed = hash_password("s3cretpass")

    assert hashed != "s3cretpass"
    assert verify_password("s3cretpass", hashed)
    assert not verify_password("other-pass", hashed)
    assert not verify_password("s3cretpass", "not-a-bcrypt-hash")


def test_decode_access_token_rejects_garbage():
    assert decode_access_token("garbage") is None


def test_webhook_signature_verification():
    body = b'{"event_id": "e1"}'
    good = sign_webhook(body)

    assert verify_webhook_signature(body, good)
    assert not verify_webhook_signature(body, None)
    assert not verify_webhook_signature(body, "")
    assert not verify_webhook_signature(body + b" ", good)
    assert not verify_webhook_signature(body, sign_webhook(body, secret="another-secret"))
    # a header full of non-ASCII characters must be a clean rejection, not a crash
    assert not verify_webhook_signature(body, "sïgnäture-☃")
