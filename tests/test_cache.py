import fakeredis
import pytest
import redis

from app import cache
from app.config import get_settings
from tests.helpers import future


@pytest.fixture
def redis_client():
    client = fakeredis.FakeRedis()
    cache.use_client(client)
    return client


def price_of(client, centre_id):
    return client.get(f"/centres/{centre_id}").json()["tests"][0]["price"]


# --- behaviour on the endpoints -----------------------------------------------------------


def test_without_redis_nothing_is_cached_and_no_header_is_sent(client, catalogue):
    response = client.get("/centres")

    assert response.status_code == 200
    assert "X-Cache" not in response.headers


def test_second_read_is_served_from_the_cache(client, catalogue, redis_client):
    first = client.get("/centres")
    second = client.get("/centres")

    assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("MISS", "HIT")
    assert first.json() == second.json()
    assert first.json()["items"][0]["tests"][0]["price"] == "500.00"


def test_cached_and_uncached_responses_are_identical(client, catalogue, redis_client):
    urls = ("/centres", f"/centres/{catalogue.centre_id}", "/tests")
    cache.use_client(None)
    uncached = [client.get(url).json() for url in urls]
    cache.use_client(redis_client)
    for url in urls:  # warm the cache
        client.get(url)

    cached = [client.get(url) for url in urls]

    assert [r.headers["X-Cache"] for r in cached] == ["HIT"] * 3
    assert [r.json() for r in cached] == uncached


def test_different_queries_are_cached_separately(client, catalogue, redis_client):
    client.get("/centres?q=koramangala")

    assert client.get("/centres?q=koramangala").headers["X-Cache"] == "HIT"
    other = client.get("/centres?q=nowhere")
    assert other.headers["X-Cache"] == "MISS"
    assert other.json()["items"] == []
    assert client.get("/centres?limit=1").headers["X-Cache"] == "MISS"


def test_changing_a_price_is_visible_immediately(client, catalogue, admin_headers, redis_client):
    assert price_of(client, catalogue.centre_id) == "500.00"
    assert price_of(client, catalogue.centre_id) == "500.00"  # now cached

    client.put(
        f"/centres/{catalogue.centre_id}/tests/{catalogue.test_id}", json={"price": "650.00"}, headers=admin_headers
    )

    assert price_of(client, catalogue.centre_id) == "650.00"
    assert client.get("/centres").json()["items"][0]["tests"][0]["price"] == "650.00"


def test_creating_and_renaming_are_visible_immediately(client, catalogue, admin_headers, redis_client):
    assert client.get("/tests").json()["total"] == 1
    assert client.get("/centres").json()["total"] == 1

    client.post("/tests", json={"name": "HbA1c"}, headers=admin_headers)
    client.post("/centres", json={"name": "EVE Indiranagar", "location": "Bengaluru"}, headers=admin_headers)
    client.patch(f"/centres/{catalogue.centre_id}", json={"name": "EVE Koramangala 2"}, headers=admin_headers)

    assert client.get("/tests").json()["total"] == 2
    centres = client.get("/centres").json()
    assert centres["total"] == 2
    assert "EVE Koramangala 2" in [c["name"] for c in centres["items"]]
    assert client.get(f"/centres/{catalogue.centre_id}").json()["name"] == "EVE Koramangala 2"


def test_a_rejected_write_does_not_invalidate(client, catalogue, admin_headers, redis_client):
    client.get("/tests")

    duplicate = client.post("/tests", json={"name": "Lipid Profile"}, headers=admin_headers)

    assert duplicate.status_code == 409
    assert client.get("/tests").headers["X-Cache"] == "HIT"


def test_a_missing_centre_is_not_cached(client, admin_headers, redis_client):
    assert client.get("/centres/1").status_code == 404
    assert client.get("/centres/1").status_code == 404
    assert redis_client.keys("catalogue:v*:centre:*") == []

    client.post("/centres", json={"name": "New", "location": "Here"}, headers=admin_headers)
    assert client.get("/centres/1").status_code == 200  # created since; the 404 was not remembered


def test_bookings_use_the_database_price_not_a_cached_one(client, catalogue, admin_headers, user_headers, redis_client):
    client.get("/centres")  # cache the 500.00 price
    client.put(
        f"/centres/{catalogue.centre_id}/tests/{catalogue.test_id}", json={"price": "700.00"}, headers=admin_headers
    )

    created = client.post(
        "/bookings",
        json={"centre_id": catalogue.centre_id, "test_id": catalogue.test_id, "appointment_at": future()},
        headers=user_headers,
    )

    assert created.json()["amount"] == "700.00"


def test_entries_expire(client, catalogue, redis_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "catalogue_cache_ttl_seconds", 30)
    client.get("/tests")

    keys = redis_client.keys("catalogue:v*:tests:*")
    assert len(keys) == 1
    assert 0 < redis_client.ttl(keys[0]) <= 30


# --- Redis failing ------------------------------------------------------------------------


class BrokenRedis:
    """Every command fails, like an unreachable server."""

    calls = 0

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            BrokenRedis.calls += 1
            raise redis.ConnectionError("connection refused")

        return fail


@pytest.fixture
def broken_redis():
    BrokenRedis.calls = 0
    cache.use_client(BrokenRedis())


def test_the_api_keeps_working_when_redis_is_down(client, catalogue, admin_headers, broken_redis):
    assert client.get("/centres").status_code == 200
    assert client.get(f"/centres/{catalogue.centre_id}").json()["name"] == "EVE Koramangala"
    write = client.post("/tests", json={"name": "HbA1c"}, headers=admin_headers)
    assert write.status_code == 201


def test_a_dead_redis_is_left_alone_for_a_few_seconds(client, catalogue, broken_redis, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache, "clock", lambda: now[0])

    client.get("/centres")
    assert BrokenRedis.calls == 1

    for _ in range(5):
        client.get("/centres")
    assert BrokenRedis.calls == 1  # not retried on every request

    now[0] += cache.RETRY_AFTER_FAILURE_SECONDS + 1
    client.get("/centres")
    assert BrokenRedis.calls == 2  # tried again after the pause


def test_caching_resumes_after_redis_comes_back(client, catalogue, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache, "clock", lambda: now[0])
    cache.use_client(BrokenRedis())
    client.get("/centres")

    cache.use_client(fakeredis.FakeRedis())
    now[0] += cache.RETRY_AFTER_FAILURE_SECONDS + 1

    assert client.get("/centres").headers["X-Cache"] == "MISS"
    assert client.get("/centres").headers["X-Cache"] == "HIT"


def test_a_corrupt_entry_is_treated_as_a_miss(client, catalogue, redis_client):
    client.get("/tests")
    (key,) = redis_client.keys("catalogue:v*:tests:*")
    redis_client.set(key, b"{not json")

    response = client.get("/tests")

    assert response.status_code == 200
    assert response.json()["total"] == 1
