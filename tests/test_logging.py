import json
import logging
import re
import uuid

from app.logging_config import JsonFormatter, log_event, request_id_var
from tests.helpers import future


def make_record(level=logging.INFO, fields=None, exc_info=None):
    logger = logging.getLogger("tests.json")
    extra = {"fields": fields} if fields is not None else None
    return logger.makeRecord("tests.json", level, __file__, 1, "payment_created", (), exc_info, extra=extra)


def test_formatter_emits_one_json_object_with_the_event_fields():
    line = JsonFormatter().format(make_record(logging.WARNING, {"event": "payment_created", "amount": "500.00"}))

    entry = json.loads(line)  # a single valid JSON document
    assert "\n" not in line
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "tests.json"
    assert entry["message"] == "payment_created"
    assert entry["event"] == "payment_created"
    assert entry["amount"] == "500.00"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$", entry["timestamp"])
    assert "request_id" not in entry  # not inside a request


def test_formatter_copes_with_values_json_cannot_natively_hold():
    identifier = uuid.uuid4()

    entry = json.loads(JsonFormatter().format(make_record(fields={"payment_id": identifier})))

    assert entry["payment_id"] == str(identifier)


def test_formatter_includes_exception_details():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        entry = json.loads(JsonFormatter().format(make_record(logging.ERROR, exc_info=sys.exc_info())))

    assert "ValueError: boom" in entry["exception"]


def test_log_event_puts_the_event_name_in_the_message_and_fields(caplog):
    caplog.set_level(logging.INFO)

    log_event(logging.getLogger("tests.events"), logging.INFO, "thing_happened", count=3)

    record = caplog.records[-1]
    assert record.getMessage() == "thing_happened"
    assert record.fields == {"event": "thing_happened", "count": 3}


# --- request ids --------------------------------------------------------------------------


def test_every_response_carries_a_request_id(client):
    response = client.get("/health")

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


def test_a_sensible_caller_supplied_request_id_is_kept(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-abc.123_x"})

    assert response.headers["X-Request-ID"] == "trace-abc.123_x"


def test_unsafe_request_ids_are_replaced(client):
    for bad in ("has spaces in it", "x" * 65, "semi;colon"):
        response = client.get("/health", headers={"X-Request-ID": bad})

        assert response.headers["X-Request-ID"] != bad
        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


def test_each_request_is_logged_once_with_its_outcome(client, caplog):
    caplog.set_level(logging.INFO)

    response = client.get("/centres", params={"limit": 1})

    access = [r for r in caplog.records if r.name == "app.access"]
    assert len(access) == 1
    fields = access[0].fields
    assert (fields["method"], fields["path"], fields["status"]) == ("GET", "/centres", 200)
    assert fields["duration_ms"] >= 0
    assert access[0].request_id == response.headers["X-Request-ID"]


def test_query_strings_are_not_logged(client, caplog):
    caplog.set_level(logging.INFO)

    client.get("/centres", params={"q": "secret-search-term"})

    access = [r for r in caplog.records if r.name == "app.access"][0]
    assert "secret-search-term" not in json.dumps(access.fields)


def test_business_events_share_the_request_id_of_the_request_that_caused_them(
    client, user_headers, catalogue, caplog
):
    caplog.set_level(logging.INFO)

    response = client.post(
        "/bookings",
        json={"centre_id": catalogue.centre_id, "test_id": catalogue.test_id, "appointment_at": future()},
        headers={**user_headers, "X-Request-ID": "trace-booking-1"},
    )

    assert response.status_code == 201
    created = [r for r in caplog.records if r.getMessage() == "booking_created"][0]
    assert created.request_id == "trace-booking-1"
    assert created.fields["amount"] == "500.00"


def test_the_request_id_does_not_leak_after_the_request(client):
    client.get("/health", headers={"X-Request-ID": "trace-leak-check"})

    assert request_id_var.get() is None


def test_failed_requests_are_logged_with_their_status(client, caplog):
    caplog.set_level(logging.INFO)

    client.get("/bookings")  # no token -> 401

    access = [r for r in caplog.records if r.name == "app.access"][0]
    assert access.fields["status"] == 401
