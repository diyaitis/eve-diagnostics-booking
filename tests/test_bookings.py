import uuid

from tests.helpers import future, pay


def book(client, headers, catalogue, **overrides):
    payload = {
        "centre_id": catalogue.centre_id,
        "test_id": catalogue.test_id,
        "appointment_at": future(),
        **overrides,
    }
    return client.post("/bookings", json=payload, headers=headers)


def test_authenticated_user_can_book_a_test(client, user_headers, catalogue):
    response = book(client, user_headers, catalogue)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["amount"] == "500.00"
    assert body["centre_name"] == "EVE Koramangala"
    assert body["test_name"] == "Lipid Profile"
    assert body["payments"] == []
    assert uuid.UUID(body["id"])


def test_booking_requires_authentication(client, catalogue):
    assert book(client, {}, catalogue).status_code == 401


def test_amount_comes_from_the_centre_price_not_the_client(client, user_headers, catalogue):
    response = book(client, user_headers, catalogue, amount="1.00")

    assert response.json()["amount"] == "500.00"


def test_price_changes_do_not_alter_existing_bookings(client, user_headers, admin_headers, catalogue):
    booking = book(client, user_headers, catalogue).json()

    client.put(
        f"/centres/{catalogue.centre_id}/tests/{catalogue.test_id}", json={"price": "999.00"}, headers=admin_headers
    )

    assert client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["amount"] == "500.00"
    assert book(client, user_headers, catalogue).json()["amount"] == "999.00"


def test_cannot_book_a_test_the_centre_does_not_offer(client, user_headers, admin_headers, catalogue):
    other_test = client.post("/tests", json={"name": "Vitamin D"}, headers=admin_headers).json()

    assert book(client, user_headers, catalogue, test_id=other_test["id"]).status_code == 404
    assert book(client, user_headers, catalogue, centre_id=9999).status_code == 404
    assert book(client, user_headers, catalogue, test_id=9999).status_code == 404


def test_appointment_must_be_in_the_future(client, user_headers, catalogue):
    assert book(client, user_headers, catalogue, appointment_at=future(days=-1)).status_code == 422


def test_appointment_must_carry_a_timezone(client, user_headers, catalogue):
    assert book(client, user_headers, catalogue, appointment_at="2099-01-01T10:00:00").status_code == 422
    assert book(client, user_headers, catalogue, appointment_at="2099-01-01T10:00:00+05:30").status_code == 201


def test_invalid_booking_payloads_are_rejected(client, user_headers, catalogue):
    assert client.post("/bookings", json={}, headers=user_headers).status_code == 422
    assert book(client, user_headers, catalogue, centre_id="abc").status_code == 422
    assert book(client, user_headers, catalogue, appointment_at="tomorrow").status_code == 422


def test_users_only_see_their_own_bookings(client, user_headers, other_headers, catalogue):
    mine = book(client, user_headers, catalogue).json()
    book(client, other_headers, catalogue)

    listing = client.get("/bookings", headers=user_headers).json()

    assert listing["total"] == 1
    assert listing["items"][0]["id"] == mine["id"]


def test_booking_list_supports_status_filter_and_pagination(client, user_headers, catalogue):
    first = book(client, user_headers, catalogue).json()
    book(client, user_headers, catalogue)
    client.post(f"/bookings/{first['id']}/cancel", headers=user_headers)

    everything = client.get("/bookings", headers=user_headers).json()
    cancelled = client.get("/bookings", params={"status": "CANCELLED"}, headers=user_headers).json()
    page = client.get("/bookings", params={"limit": 1}, headers=user_headers).json()

    assert everything["total"] == 2
    assert [b["id"] for b in cancelled["items"]] == [first["id"]]
    assert len(page["items"]) == 1 and page["total"] == 2
    assert client.get("/bookings", params={"status": "BOGUS"}, headers=user_headers).status_code == 422


def test_someone_elses_booking_looks_like_it_does_not_exist(client, user_headers, other_headers, catalogue):
    booking = book(client, user_headers, catalogue).json()

    assert client.get(f"/bookings/{booking['id']}", headers=other_headers).status_code == 404
    assert client.post(f"/bookings/{booking['id']}/cancel", headers=other_headers).status_code == 404
    # ...and their attempt changed nothing
    assert client.get(f"/bookings/{booking['id']}", headers=user_headers).json()["status"] == "PENDING"


def test_invalid_or_unknown_booking_ids(client, user_headers):
    assert client.get("/bookings/not-a-uuid", headers=user_headers).status_code == 422
    assert client.get(f"/bookings/{uuid.uuid4()}", headers=user_headers).status_code == 404
    assert client.post(f"/bookings/{uuid.uuid4()}/cancel", headers=user_headers).status_code == 404


def test_cancel_pending_booking(client, user_headers, booking):
    response = client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_cancelling_twice_is_a_conflict(client, user_headers, booking):
    client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    assert client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers).status_code == 409


def test_confirmed_booking_can_be_cancelled(client, user_headers, booking):
    pay(client, user_headers, booking["id"], "SUCCESS")

    response = client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers)

    assert response.json()["status"] == "CANCELLED"


def test_failed_booking_cannot_be_cancelled(client, user_headers, booking):
    pay(client, user_headers, booking["id"], "FAILED")

    assert client.post(f"/bookings/{booking['id']}/cancel", headers=user_headers).status_code == 409
