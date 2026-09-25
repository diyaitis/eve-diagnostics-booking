import pytest


def make_centre(client, headers, name="EVE Indiranagar", location="Bengaluru"):
    response = client.post("/centres", json={"name": name, "location": location}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def make_test(client, headers, name="HbA1c"):
    response = client.post("/tests", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_anyone_can_browse_centres_with_their_tests_and_prices(client, catalogue):
    response = client.get("/centres")  # no authentication needed

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    centre = body["items"][0]
    assert centre["name"] == "EVE Koramangala"
    assert centre["location"] == "Bengaluru"
    assert centre["tests"] == [{"test_id": catalogue.test_id, "name": "Lipid Profile", "price": "500.00"}]


def test_get_single_centre_and_unknown_centre(client, catalogue):
    assert client.get(f"/centres/{catalogue.centre_id}").json()["name"] == "EVE Koramangala"
    assert client.get("/centres/99999").status_code == 404
    assert client.get("/centres/not-a-number").status_code == 422


def test_centres_are_paginated(client, admin_headers):
    for index in range(5):
        make_centre(client, admin_headers, name=f"Centre {index}")

    first = client.get("/centres", params={"limit": 2}).json()
    last = client.get("/centres", params={"limit": 2, "offset": 4}).json()

    assert first["total"] == 5 and len(first["items"]) == 2
    assert [c["name"] for c in first["items"]] == ["Centre 0", "Centre 1"]
    assert [c["name"] for c in last["items"]] == ["Centre 4"]
    assert (first["limit"], first["offset"]) == (2, 0)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "many"}])
def test_invalid_pagination_is_rejected(client, params):
    assert client.get("/centres", params=params).status_code == 422


def test_search_centres_by_text_and_by_offered_test(client, admin_headers, catalogue):
    mumbai = make_centre(client, admin_headers, name="EVE Bandra", location="Mumbai")
    other_test = make_test(client, admin_headers, name="Vitamin D")
    client.put(f"/centres/{mumbai['id']}/tests/{other_test['id']}", json={"price": "900"}, headers=admin_headers)

    by_text = client.get("/centres", params={"q": "mumbai"}).json()
    by_test = client.get("/centres", params={"test_id": catalogue.test_id}).json()

    assert [c["name"] for c in by_text["items"]] == ["EVE Bandra"]
    assert [c["name"] for c in by_test["items"]] == ["EVE Koramangala"]


def test_search_treats_wildcards_literally(client, catalogue):
    assert client.get("/centres", params={"q": "%"}).json()["total"] == 0
    assert client.get("/centres", params={"q": "_"}).json()["total"] == 0


def test_list_and_search_tests(client, admin_headers):
    make_test(client, admin_headers, "Complete Blood Count")
    make_test(client, admin_headers, "Lipid Profile")

    everything = client.get("/tests").json()
    searched = client.get("/tests", params={"q": "lipid"}).json()

    assert everything["total"] == 2
    assert [t["name"] for t in searched["items"]] == ["Lipid Profile"]


def test_only_admins_can_change_the_catalogue(client, user_headers, catalogue):
    centre = {"name": "New", "location": "Pune"}
    price = {"price": "10"}
    path = f"/centres/{catalogue.centre_id}/tests/{catalogue.test_id}"

    # not logged in -> 401
    assert client.post("/centres", json=centre).status_code == 401
    assert client.post("/tests", json={"name": "X"}).status_code == 401
    assert client.put(path, json=price).status_code == 401
    # logged in but not an admin -> 403
    assert client.post("/centres", json=centre, headers=user_headers).status_code == 403
    assert client.patch(f"/centres/{catalogue.centre_id}", json={"name": "Z"}, headers=user_headers).status_code == 403
    assert client.post("/tests", json={"name": "X"}, headers=user_headers).status_code == 403
    assert client.put(path, json=price, headers=user_headers).status_code == 403
    # and nothing changed
    assert client.get(f"/centres/{catalogue.centre_id}").json()["tests"][0]["price"] == "500.00"


def test_duplicate_centre_and_test_are_rejected(client, admin_headers):
    make_centre(client, admin_headers, "Same", "Place")
    make_test(client, admin_headers, "Same Test")

    assert client.post("/centres", json={"name": "Same", "location": "Place"}, headers=admin_headers).status_code == 409
    assert client.post("/tests", json={"name": "Same Test"}, headers=admin_headers).status_code == 409
    # the same name in a different location is fine
    assert client.post("/centres", json={"name": "Same", "location": "Elsewhere"}, headers=admin_headers).status_code == 201


def test_centre_and_test_validation(client, admin_headers):
    assert client.post("/centres", json={"name": "", "location": "X"}, headers=admin_headers).status_code == 422
    assert client.post("/centres", json={"name": "X"}, headers=admin_headers).status_code == 422
    assert client.post("/tests", json={"name": "   "}, headers=admin_headers).status_code == 422


@pytest.mark.parametrize("price", ["-1", "10.999", "abc", "12345678901", None])
def test_invalid_prices_are_rejected(client, admin_headers, catalogue, price):
    path = f"/centres/{catalogue.centre_id}/tests/{catalogue.test_id}"

    assert client.put(path, json={"price": price}, headers=admin_headers).status_code == 422


def test_price_can_be_changed_and_zero_is_allowed(client, admin_headers, catalogue):
    path = f"/centres/{catalogue.centre_id}/tests/{catalogue.test_id}"

    changed = client.put(path, json={"price": "650.5"}, headers=admin_headers).json()
    free = client.put(path, json={"price": "0"}, headers=admin_headers).json()

    assert changed["tests"][0]["price"] == "650.50"
    assert free["tests"][0]["price"] == "0.00"
    assert len(free["tests"]) == 1  # updated in place, not duplicated


def test_offering_requires_an_existing_centre_and_test(client, admin_headers, catalogue):
    assert client.put(f"/centres/9999/tests/{catalogue.test_id}", json={"price": "1"}, headers=admin_headers).status_code == 404
    assert client.put(f"/centres/{catalogue.centre_id}/tests/9999", json={"price": "1"}, headers=admin_headers).status_code == 404


def test_update_centre(client, admin_headers, catalogue):
    path = f"/centres/{catalogue.centre_id}"

    renamed = client.patch(path, json={"name": "EVE Koramangala 2"}, headers=admin_headers)

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "EVE Koramangala 2"
    assert renamed.json()["location"] == "Bengaluru"  # untouched
    assert client.patch(path, json={}, headers=admin_headers).status_code == 422
    assert client.patch("/centres/9999", json={"name": "X"}, headers=admin_headers).status_code == 404
