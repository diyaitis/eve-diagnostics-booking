import logging

import pytest

from app.config import get_settings
from app.ratelimit import RateLimiter, login_limit, reset_all_limiters, signup_limit, webhook_limit
from tests.helpers import event, send_webhook


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# --- the limiter itself -------------------------------------------------------------------


def test_allows_up_to_the_limit_then_reports_how_long_to_wait():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window=60, clock=clock)

    assert [limiter.check("ip") for _ in range(3)] == [None, None, None]

    clock.advance(20)
    assert limiter.check("ip") == pytest.approx(40)  # the oldest hit expires 40s from now


def test_the_window_slides_so_capacity_returns_gradually():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window=60, clock=clock)
    limiter.check("ip")  # t=0
    clock.advance(30)
    limiter.check("ip")  # t=30
    assert limiter.check("ip") is not None

    clock.advance(31)  # t=61: only the first hit has expired
    assert limiter.check("ip") is None
    assert limiter.check("ip") is not None


def test_refused_hits_do_not_extend_the_wait():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window=60, clock=clock)
    limiter.check("ip")

    for _ in range(50):  # hammering while blocked
        clock.advance(1)
        assert limiter.check("ip") is not None

    clock.advance(10)  # t=60 since the one recorded hit
    assert limiter.check("ip") is None


def test_clients_are_counted_separately():
    limiter = RateLimiter(limit=1, window=60, clock=FakeClock())

    assert limiter.check("10.0.0.1") is None
    assert limiter.check("10.0.0.2") is None
    assert limiter.check("10.0.0.1") is not None


def test_idle_clients_are_forgotten_so_memory_stays_bounded():
    clock = FakeClock()
    limiter = RateLimiter(limit=1000, window=60, clock=clock)
    for index in range(500):
        limiter.check(f"client-{index}")
    clock.advance(120)  # all of them are now idle

    for _ in range(500):  # the 1000th recorded hit triggers the cleanup
        limiter.check("active-client")

    assert set(limiter._hits) == {"active-client"}


# --- on the endpoints ---------------------------------------------------------------------


@pytest.fixture
def limits_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    reset_all_limiters()
    yield
    reset_all_limiters()


def attempt_login(client, password="wrong-password"):
    return client.post("/auth/login", data={"username": "patient@example.com", "password": password})


def test_repeated_logins_are_throttled_with_a_retry_after(client, user_headers, limits_on):
    reset_all_limiters()  # the fixture that created the user already used one login

    statuses = [attempt_login(client).status_code for _ in range(10)]
    blocked = attempt_login(client)

    assert statuses == [401] * 10
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests, please slow down"}
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60


def test_a_throttled_client_is_refused_even_with_the_right_password(client, user_headers, limits_on):
    reset_all_limiters()
    for _ in range(10):
        attempt_login(client)

    assert attempt_login(client, password="s3cretpass").status_code == 429


def test_logins_work_again_once_the_window_has_passed(client, user_headers, limits_on, monkeypatch):
    reset_all_limiters()
    clock = FakeClock()
    monkeypatch.setattr(login_limit.limiter, "clock", clock)
    for _ in range(10):
        attempt_login(client)
    assert attempt_login(client).status_code == 429

    clock.advance(61)

    assert attempt_login(client, password="s3cretpass").status_code == 200


def test_signup_is_throttled_too(client, limits_on):
    codes = [
        client.post("/auth/signup", json={"email": f"user{i}@example.com", "password": "s3cretpass", "full_name": "U"}).status_code
        for i in range(11)
    ]

    assert codes == [201] * 10 + [429]


def test_the_webhook_is_throttled_before_its_signature_is_even_checked(client, limits_on, monkeypatch):
    monkeypatch.setattr(webhook_limit.limiter, "limit", 3)

    codes = [send_webhook(client, event("e", "pay_x"), signature="bad").status_code for _ in range(4)]

    assert codes == [401, 401, 401, 429]


def test_other_endpoints_are_not_affected(client, user_headers, limits_on):
    reset_all_limiters()
    for _ in range(11):
        attempt_login(client)

    assert client.get("/centres").status_code == 200
    assert client.get("/auth/me", headers=user_headers).status_code == 200


def test_limiting_can_be_switched_off(client, user_headers):
    # rate limiting is off in the test environment unless the limits_on fixture enables it
    assert [attempt_login(client).status_code for _ in range(15)] == [401] * 15


def test_throttling_is_logged(client, user_headers, limits_on, caplog):
    reset_all_limiters()
    caplog.set_level(logging.WARNING)
    for _ in range(11):
        attempt_login(client)

    limited = [r for r in caplog.records if r.getMessage() == "rate_limited"]
    assert len(limited) == 1
    assert limited[0].fields["path"] == "/auth/login"


def test_the_limits_are_sensible_defaults():
    assert signup_limit.limiter.limit == 10
    assert login_limit.limiter.limit == 10
    assert webhook_limit.limiter.limit == 120
