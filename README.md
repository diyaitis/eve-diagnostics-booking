# EVE Diagnostics Booking API

A small backend for booking diagnostic tests at diagnostic centres, with a simulated payment
provider and an idempotent payment webhook.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL · JWT (PyJWT) · bcrypt · pytest · Docker Compose

Swagger UI is served at `/docs` (with an **Authorize** button) and the OpenAPI schema at `/openapi.json`.

## Contents

1. [Quick start](#quick-start-docker) · [Running without Docker](#running-the-api-without-docker) · [Tests](#running-the-tests)
2. [API and example requests](#api)
3. [Database design](#database-design)
4. [Booking and payment flow](#booking-and-payment-flow)
5. [How the webhook stays idempotent](#how-the-webhook-stays-idempotent)
6. [Edge cases handled](#edge-cases-handled)
7. [Assumptions](#assumptions) · [What I would improve](#what-i-would-improve-with-more-time)

---

## Quick start (Docker)

Requires Docker with Compose.

```bash
docker compose up --build -d          # PostgreSQL + the API on http://localhost:8000
docker compose exec api python -m app.seed   # demo centres/tests + an admin user
```

Open http://localhost:8000/docs.

The seed script creates an admin for local use only: `admin@example.com` / `admin12345`
(override with `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD`). Normal signups are never admins.

Stop with `docker compose down` (add `-v` to also delete the database volume).

## Running the API without Docker

Only PostgreSQL needs to be in a container; the API runs from a virtualenv.

```bash
docker compose up -d db                      # PostgreSQL on localhost:5433
python -m venv .venv
source .venv/bin/activate                    # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                         # then edit the secrets
uvicorn app.main:app --reload
python -m app.seed                           # optional demo data
```

Tables are created automatically on startup. Configuration is by environment variable (see
[`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://eve:eve@localhost:5432/eve` | Database connection |
| `JWT_SECRET` | dev placeholder | **Change in production** (use 32+ random bytes) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | Token lifetime |
| `WEBHOOK_SECRET` | `dev-webhook-secret` | Shared secret for webhook signatures |
| `PAYMENT_SUCCESS_RATE` | `0.8` | Chance the simulated provider approves a payment |
| `ALLOW_SIMULATED_OUTCOME` | `true` | Allow forcing an outcome via `simulate_outcome` |
| `RATE_LIMIT_ENABLED` | `true` | Turn the login/signup/webhook rate limits on or off |
| `LOG_FORMAT` | `json` | `json` (one object per line) or `text` (easier to read while developing) |
| `LOG_LEVEL` | `INFO` | Standard Python log level |

## Running the tests

144 tests: unit tests for the state machine, security, rate limiter and retry helpers, and API-level tests for
every endpoint and edge case.

```bash
# Fast: in-memory SQLite (141 tests run, 3 are skipped - see below)
pytest

# Full: real PostgreSQL, including the concurrency tests
docker compose up -d db
TEST_DATABASE_URL=postgresql+psycopg://eve:eve@localhost:5433/eve_test pytest

# Full, entirely in containers
docker compose --profile test run --rm tests
```

The 3 skipped tests fire many simultaneous requests at the same webhook / payment / booking and
need PostgreSQL's row locks and unique constraints. The tests use `TEST_DATABASE_URL` (never
`DATABASE_URL`) and refuse to run against a database whose name doesn't contain `test`, so they
cannot wipe a real database by accident.

---

## API

`Auth`: **public** = no token · **user** = `Authorization: Bearer <token>` · **admin** = user with `is_admin`.
List endpoints take `limit` (1-100, default 20) and `offset`, and return
`{"items": [...], "total": N, "limit": L, "offset": O}`.

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /auth/signup` | public | Create an account |
| `POST /auth/login` | public | Get a JWT (form fields `username` = email, `password`) |
| `GET /auth/me` | user | Current user |
| `GET /centres` | public | List centres with their tests and prices. Filters: `q` (name/location), `test_id` |
| `GET /centres/{id}` | public | One centre |
| `POST /centres` | admin | Create a centre |
| `PATCH /centres/{id}` | admin | Rename / relocate a centre |
| `PUT /centres/{id}/tests/{test_id}` | admin | Offer a test at a price (or change the price) |
| `GET /tests` | public | List the test catalogue. Filter: `q` |
| `POST /tests` | admin | Add a test to the catalogue |
| `POST /bookings` | user | Book a test at a centre |
| `GET /bookings` | user | My bookings. Filter: `status` |
| `GET /bookings/{id}` | user | One of my bookings (with its payments) |
| `POST /bookings/{id}/cancel` | user | Cancel a booking |
| `POST /payments/` | user | Pay for a booking through the simulated provider |
| `GET /payments/{id}` | user | One of my payments |
| `POST /payments/webhook/` | HMAC signature | Payment-status updates from the provider |
| `GET /health` | public | Liveness check |

### Example: the whole journey with `curl`

```bash
B=http://localhost:8000

# 1. Sign up and log in
curl -s -X POST $B/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"asha@example.com","password":"s3cretpass","full_name":"Asha Rao"}'
TOKEN=$(curl -s -X POST $B/auth/login -d 'username=asha@example.com&password=s3cretpass' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"

# 2. Browse centres (public)
curl -s "$B/centres?q=bengaluru"

# 3. Book test 1 at centre 1. The amount comes from the centre's price, never from the client.
curl -s -X POST $B/bookings -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"centre_id":1,"test_id":1,"appointment_at":"2099-01-15T10:30:00+05:30"}'
# -> {"id":"<booking-id>","status":"PENDING","amount":"350.00", ...}

# 4a. Pay. The simulated provider approves ~80% of the time...
curl -s -X POST $B/payments/ -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"booking_id":"<booking-id>"}'
# -> 201 {"status":"SUCCESS", ...}  booking becomes CONFIRMED
#    or   {"status":"FAILED", ...}   booking becomes FAILED

# 4b. ...or force an outcome for testing. "PENDING" means "accepted, settles later via webhook":
curl -s -X POST $B/payments/ -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"booking_id":"<booking-id>","simulate_outcome":"PENDING"}'
# -> {"status":"PENDING","provider_reference":"pay_c28a89...", ...}

# 5. The provider then calls the webhook. The body is signed with HMAC-SHA256 (hex) in a header:
BODY='{"event_id":"evt_001","provider_reference":"pay_c28a89...","status":"SUCCESS"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac 'dev-webhook-secret' | awk '{print $NF}')
curl -s -X POST $B/payments/webhook/ -H "X-Webhook-Signature: $SIG" \
  -H 'Content-Type: application/json' -d "$BODY"
# -> {"event_id":"evt_001","result":"APPLIED","duplicate":false}
# Sending it again is harmless:
# -> {"event_id":"evt_001","result":"APPLIED","duplicate":true}
```

Notes on the responses:

* Money is a string with two decimals (`"350.00"`), never a float.
* A **declined payment is not an HTTP error**: `POST /payments/` returns `201` with `"status": "FAILED"`,
  because a payment was created and processed. Errors (`401/403/404/409/422`) are for invalid requests.
* Errors are JSON: `{"detail": "..."}`; validation errors use FastAPI's standard `422` shape.
* Too many requests to login, signup or the webhook: `429` with a `Retry-After` header. If the database is briefly
  unavailable the webhook answers `503` with `Retry-After` (see [retry handling](#how-the-webhook-stays-idempotent)).
* Every response has an `X-Request-ID` header; send your own (letters, digits, `._-`, up to 64 characters) to trace a call.

### Webhook result values

| `result` | Meaning |
|---|---|
| `APPLIED` | The event changed the payment (and normally the booking) |
| `NO_CHANGE` | The payment was already in the reported state |
| `IGNORED` | The payment was already final in a *different* state; nothing was changed |

`duplicate: true` means this `event_id` had been processed before; `result` is the stored original result.

---

## Database design

```mermaid
erDiagram
    users ||--o{ bookings : makes
    centres ||--o{ centre_tests : offers
    diagnostic_tests ||--o{ centre_tests : "is offered as"
    centre_tests ||--o{ bookings : "is booked as"
    bookings ||--o{ payments : "is paid by"
    payments ||--o{ webhook_events : "is updated by"

    users {
        uuid id PK
        string email UK
        string full_name
        string hashed_password
        bool is_admin
    }
    centres {
        int id PK
        string name "unique with location"
        string location
    }
    diagnostic_tests {
        int id PK
        string name UK
        text description
    }
    centre_tests {
        int centre_id PK,FK
        int test_id PK,FK
        numeric price "CHECK >= 0"
    }
    bookings {
        uuid id PK
        uuid user_id FK
        int centre_id FK "composite FK to centre_tests"
        int test_id FK "composite FK to centre_tests"
        timestamptz appointment_at
        numeric amount "snapshot of the price"
        string status "PENDING CONFIRMED FAILED CANCELLED"
    }
    payments {
        uuid id PK
        uuid booking_id FK
        numeric amount
        string status "PENDING SUCCESS FAILED"
        string provider_reference UK
    }
    webhook_events {
        int id PK
        string event_id UK "the idempotency key"
        uuid payment_id FK
        json payload
        string result
    }
```

Design decisions worth knowing:

* **Price belongs to `centre_tests`**, not to the test: the same test costs different amounts at different centres.
* **`bookings.amount` is a snapshot.** Changing a price later never changes what an existing booking costs.
* **Composite foreign key `bookings(centre_id, test_id) → centre_tests`.** The database itself guarantees a
  booking can only exist for a test that centre actually offers.
* **Partial unique index on `payments(booking_id) WHERE status IN ('PENDING','SUCCESS')`.** At most one live
  payment per booking, enforced by the database, so it holds even if two requests race. A `FAILED` payment
  does not count, which leaves room for a retry policy later.
* **`webhook_events.event_id` is unique** - this constraint is what makes the webhook idempotent.
* **Statuses are `VARCHAR` + `CHECK`**, not native enums (easier to evolve). Timestamps are `timestamptz`, always UTC.
* **Money is `NUMERIC(10,2)`**, never float. Public IDs of private resources (users, bookings, payments) are UUIDs so
  they can't be guessed; catalogue IDs are small integers.

---

## Booking and payment flow

```mermaid
stateDiagram-v2
    [*] --> PENDING: POST /bookings
    PENDING --> CONFIRMED: payment SUCCESS
    PENDING --> FAILED: payment FAILED
    PENDING --> CANCELLED: cancel
    CONFIRMED --> CANCELLED: cancel
    FAILED --> [*]
    CANCELLED --> [*]
```

`FAILED` and `CANCELLED` are final. The allowed transitions live in one table
(`ALLOWED_TRANSITIONS` in `app/services/bookings.py`), and every status change goes through
`transition()`, so an illegal move is impossible rather than merely unlikely.

A **payment** only moves `PENDING → SUCCESS` or `PENDING → FAILED`, and never again after that. The same function
(`apply_payment_result`) applies an outcome whether it came from `POST /payments/` or from the webhook.

## How the webhook stays idempotent

1. **Authenticity first.** The body must carry a valid HMAC-SHA256 signature (`X-Webhook-Signature`, computed over the
   exact request bytes and compared in constant time). Otherwise `401`, and nothing is touched.
2. **Insert the event, then act - in one transaction.** The `webhook_events` row (unique `event_id`) is inserted in
   the same transaction that updates the payment and booking. If the insert collides, the event was already
   handled: the stored result is returned (`duplicate: true`) and nothing else happens.
3. **This also holds when copies arrive at the same moment.** The unique constraint is the arbiter (the loser of the
   race gets a constraint violation and reports a duplicate), not an application-level "check then insert". Rows are
   locked in a fixed order (booking, then payment) so concurrent requests can't deadlock. There is a test that fires 8
   identical webhooks simultaneously and asserts exactly one is applied.
4. **A final payment never changes.** A later or contradictory event with a *different* `event_id` (for example a
   `FAILED` that arrives after a `SUCCESS`) returns `IGNORED` and cannot corrupt the booking.
5. **Retry-friendly status codes.** An unknown payment reference returns `404` *and stores nothing*, so the provider
   can retry once the payment exists. A known payment always returns `200` - even for an ignored event - so the
   provider doesn't retry forever.
6. **Transient failures are retried.** If processing hits a deadlock, lock timeout or dropped connection, the session is
   rolled back and the event is processed again (up to 3 attempts, backoff 50 ms then 100 ms) - safe precisely because
   the handler is idempotent. If the database is still failing, the endpoint answers `503` + `Retry-After` and stores
   nothing, so the provider's own retry is handled normally. Real bugs are not retried or disguised as outages.

---

## Bonus features

| Feature | Where to look |
|---|---|
| Docker and docker-compose | `Dockerfile`, `docker-compose.yml`: the API, PostgreSQL and a containerised test run |
| Swagger / OpenAPI | `/docs` and `/openapi.json`, with an **Authorize** button |
| Unit and integration tests | `tests/`: 144 tests, including concurrency tests against PostgreSQL |
| Pagination | Every list endpoint: `limit`, `offset`, and a `total` |
| Structured logging | JSON, one object per line, with a request id on every line (`app/logging_config.py`, `app/middleware.py`) |
| Rate limiting | Login and signup 10/min, webhook 120/min, per client, `429` + `Retry-After` (`app/ratelimit.py`) |
| Webhook retry handling | Transient database errors retried with backoff, then `503` + `Retry-After` (`app/services/retry.py`) |

A log line looks like this:

```json
{"timestamp": "2026-09-25T19:03:28.273+00:00", "level": "INFO", "logger": "app.access", "message": "request", "request_id": "demo-123", "event": "request", "method": "GET", "path": "/health", "status": 200, "duration_ms": 3.3}
```

**Deliberately not included: Redis caching and Celery/background jobs.** The catalogue is tiny and read-mostly, and the
payment flow is synchronous with an idempotent webhook, so neither solves a real problem here - each would add a
service to run and operate. They are the first things I would add at scale (see below).

The rate limiter is in-memory, so it is per-process: fine for one instance, but several instances would each count
separately and need a shared store such as Redis.

## Edge cases handled

| Case | Behaviour |
|---|---|
| Invalid / missing fields, bad email, short password, negative or 3-decimal price | `422` with field-level errors |
| Missing, malformed, tampered, expired or wrongly-signed JWT | `401` |
| Non-admin changes the catalogue | `403` (anonymous: `401`) |
| Reading, cancelling or paying **someone else's** booking or payment | `404` (not `403`, so IDs can't be probed) |
| Malformed booking ID (`/bookings/abc`) | `422`; unknown but well-formed ID: `404` |
| Booking a test the centre doesn't offer / unknown centre or test | `404` |
| Appointment in the past, or without a timezone | `422` |
| Client tries to set its own price | Ignored - the amount always comes from the centre's price |
| Price changes after booking | Existing bookings keep their amount |
| Paying a booking twice, or while a payment is pending | `409` (also enforced by a DB index under races) |
| Paying a `FAILED`, `CANCELLED` or already-`CONFIRMED` booking | `409` |
| Failed payment | `201` with `status: FAILED`; booking becomes `FAILED` |
| Duplicate signup email (any letter case) | `409` |
| Login with wrong password vs. unknown email | Identical `401` (and equal work done, to avoid a timing leak) |
| Webhook: replayed `event_id`, concurrent copies | Applied once; duplicates return the stored result |
| Webhook: late / contradictory event | `IGNORED`, state unchanged |
| Webhook: bad or missing signature (including non-ASCII garbage) | `401` |
| Webhook: unknown payment, malformed or non-JSON body, `PENDING` status | `404` / `422`, nothing stored |
| Payment settles after the booking was cancelled | Payment recorded as `SUCCESS`, booking stays `CANCELLED`, warning logged with `needs_refund=true` |
| LIKE wildcards in search (`q=%`) | Treated literally |
| Brute-forcing login, mass signups, flooding the webhook URL | `429` + `Retry-After`; refused attempts don't extend the wait |
| Webhook hits a deadlock / dropped connection | Rolled back and retried; if it persists, `503` + `Retry-After` and nothing stored |
| Unsafe `X-Request-ID` (spaces, newlines, very long) | Replaced with a generated id, so logs can't be polluted |

---

## Assumptions

* **Framework/DB:** FastAPI + PostgreSQL. Tables are created on startup; there is no migration tool (see improvements).
* **Admins:** the PDF says centres can be "managed" but defines no admin role, so users have an `is_admin` flag.
  Signup can never set it; the first admin comes from the seed script.
* **Reads of the catalogue are public**; bookings and payments require login.
* **One currency**, amounts stored as decimals with two places; no currency column.
* **Simulated payments:** the provider approves with probability `PAYMENT_SUCCESS_RATE`. `simulate_outcome`
  lets a caller force `SUCCESS`, `FAILED` or `PENDING` (for demos and tests) and can be switched off with
  `ALLOW_SIMULATED_OUTCOME=false`. `PENDING` models a provider that answers later through the webhook.
* **A failed payment is final for that booking** (`FAILED` is a terminal state, as suggested in the brief); the
  user books again. Cancelling a confirmed booking has no refund flow.
* **Webhook trust:** the provider signs the body with a shared secret (HMAC-SHA256). The `event_id` is the
  provider's unique event identifier and is treated as globally unique.
* **Appointments** must be in the future and carry a UTC offset. There is no slot capacity or double-booking check.
* **Auth:** one short-lived access token (60 min, HS256). No refresh tokens or revocation. Passwords are at least 8
  characters and at most 72 bytes (a bcrypt limit).

## What I would improve with more time

* **Alembic migrations** instead of `create_all`, plus a CI pipeline running the suite on SQLite and PostgreSQL.
* **Refunds and reconciliation:** a `REFUNDED` state, a refund flow for the "paid after cancel" case, and a job that
  chases payments stuck in `PENDING`.
* **Webhook processing in the background** (Celery/Redis or a transactional outbox) with retries and a dead-letter
  queue, so a slow handler never makes the provider time out. Also key rotation for the signing secret.
* **`Idempotency-Key` header** on `POST /bookings` and `POST /payments/`, so a client retry after a network error can't
  double-submit.
* **Availability:** slots and capacity per centre and per test, opening hours, centre time zones, reminders.
* **Security hardening:** a shared (Redis) rate-limit store for multi-instance deployments and per-user limits, refresh
  tokens with revocation, account lockout.
* **Operations:** metrics and tracing, a readiness check that pings the database, Redis caching for the read-heavy
  catalogue, soft-delete of centres, and admin views of all bookings.

---

## Project structure

```
app/
  main.py          app factory, error handler, router registration
  config.py        settings from environment variables
  database.py      engine and session
  models.py        tables, constraints, enums (the schema, documented inline)
  schemas.py       request/response models and validation
  security.py      password hashing, JWT, webhook signatures
  logging_config.py  structured JSON logging, request-id context
  middleware.py    request id + one access-log line per request
  ratelimit.py     sliding-window rate limiter and its dependencies
  deps.py          auth dependencies, pagination
  errors.py        domain errors -> HTTP status codes
  routers/         thin HTTP layer (auth, catalog, bookings, payments)
  services/        business rules: bookings (state machine), payments (+ webhook), catalog, users, retry, mock provider
  seed.py          demo data and first admin
tests/             pytest suite (fixtures in conftest.py)
docker/initdb/     creates the eve_test database
Dockerfile, docker-compose.yml, requirements*.txt, .env.example
```

Routers only parse input and shape output; all rules live in `services/`, which take a database session and plain
values, so they are easy to reason about and test.
