import os

# Configure the app BEFORE it is imported. TEST_DATABASE_URL (never DATABASE_URL) is used so a
# developer's real database can't be picked up and wiped by the fixtures below.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ["JWT_SECRET"] = "test-jwt-secret-that-is-at-least-32-bytes-long"
os.environ["WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["BCRYPT_ROUNDS"] = "4"  # keep password hashing fast in tests
os.environ["ALLOW_SIMULATED_OUTCOME"] = "true"
os.environ["RATE_LIMIT_ENABLED"] = "false"  # switched on explicitly in test_ratelimit.py
os.environ.pop("REDIS_URL", None)  # a developer's own Redis must never leak into the tests
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "true"  # background tasks run inline; no broker needed

_url = os.environ["DATABASE_URL"]
if not _url.startswith("sqlite") and "test" not in _url.rsplit("/", 1)[-1]:
    raise RuntimeError(f"Refusing to run tests against {_url!r}: the database name must contain 'test'")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import cache  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.ratelimit import reset_all_limiters  # noqa: E402
from app.services import users  # noqa: E402
from tests.helpers import future, login_headers, signup_and_login  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    reset_all_limiters()
    cache.use_client(None)  # off unless a test turns it on (test_cache.py)
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def user_headers(client):
    return signup_and_login(client, "patient@example.com")


@pytest.fixture
def other_headers(client):
    return signup_and_login(client, "someone.else@example.com")


@pytest.fixture
def admin_headers(client):
    with SessionLocal() as db:
        users.create_user(db, "admin@example.com", "adminpass1", "Admin", is_admin=True)
    return login_headers(client, "admin@example.com", "adminpass1")


@pytest.fixture
def catalogue(client, admin_headers):
    """One test, offered by one centre at 500.00."""
    test = client.post("/tests", json={"name": "Lipid Profile"}, headers=admin_headers).json()
    centre = client.post(
        "/centres", json={"name": "EVE Koramangala", "location": "Bengaluru"}, headers=admin_headers
    ).json()
    response = client.put(
        f"/centres/{centre['id']}/tests/{test['id']}", json={"price": "500.00"}, headers=admin_headers
    )
    assert response.status_code == 200
    return SimpleNamespace(centre_id=centre["id"], test_id=test["id"], price="500.00")


@pytest.fixture
def booking(client, user_headers, catalogue):
    response = client.post(
        "/bookings",
        json={"centre_id": catalogue.centre_id, "test_id": catalogue.test_id, "appointment_at": future()},
        headers=user_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()
