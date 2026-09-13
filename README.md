# Callback Inspector

[![CI](https://github.com/VsevaTech/callback-inspector/actions/workflows/ci.yml/badge.svg)](https://github.com/VsevaTech/callback-inspector/actions/workflows/ci.yml)

An outbound-callback proxy that keeps **evidence** for every delivery: what was sent, where, when,
what came back, how long it took — and lets you retry a failed delivery by hand.

## The problem

Every B2B integration eventually hits this conversation:

> **Us:** We sent you the `payment.succeeded` callback for order ORD-1001 at 10:15.
> **Partner:** We never received it.

Usually nobody can prove anything. The sending service logged "callback sent" (maybe), the log
line has no response body, the request body was not stored, the retry logic is hidden inside a
queue worker, and the person on support duty has to ask a developer to grep production logs.

Callback Inspector sits between your system and the partner. Your system calls **one** endpoint,
Callback Inspector does the HTTP call to the partner and stores the whole exchange as a
first-class record:

```text
Client/System
→ POST /api/deliveries            (callback is persisted as *pending* BEFORE anything is sent)
→ Callback Inspector → Partner callback URL   (httpx, with the configured timeout)
→ save request/response/latency/status for this attempt
→ show delivery result            (delivered | failed)
→ allow manual retry if failed    (every retry is a new, fully recorded attempt)
```

So the answer to "did you send it?" becomes a link: `/deliveries/<id>` — with the exact request
headers and body, the partner's HTTP status and response body, timestamps and latency for every
attempt.

## Quick start

```bash
docker compose up --build
```

| Service | URL | What it is |
|---|---|---|
| Callback Inspector UI | http://localhost:8000 | list of deliveries, details, retry button |
| Callback Inspector API | http://localhost:8000/docs | OpenAPI / Swagger |
| Mock partner receiver | http://localhost:8001 | demo partner that answers 200 or 503 on command |

Then run the end-to-end demo (see [Demo scenario](#demo-scenario)):

```bash
bash examples/demo.sh
```

Local development without Docker:

```bash
pip install -r requirements-dev.txt
python scripts/vendor_htmx.py                           # optional: local copy of htmx (else CDN fallback)
uvicorn app.mock_receiver:receiver_app --port 8001 &   # demo partner
uvicorn app.main:app --reload --port 8000               # inspector
ruff check . && ruff format --check . && pytest -q
```

Configuration is via environment variables — see [`.env.example`](.env.example).

## API example

Create a delivery (persist → send once → return the result):

```bash
curl -X POST http://localhost:8000/api/deliveries \
  -H 'Content-Type: application/json' \
  -d '{
    "destination_url": "http://receiver:8001/callback",
    "method": "POST",
    "headers": {"X-Signature": "demo-signature"},
    "payload": {"event": "payment.succeeded", "order_id": "ORD-1001", "amount": 4200, "currency": "AED"},
    "timeout_seconds": 5
  }'
```

Response (`201 Created`):

```json
{
  "id": "336cb264c4e14d988bdd255dc5511383",
  "destination_url": "http://receiver:8001/callback",
  "method": "POST",
  "headers": {"X-Signature": "demo-signature"},
  "payload": {"event": "payment.succeeded", "order_id": "ORD-1001", "amount": 4200, "currency": "AED"},
  "timeout_seconds": 5.0,
  "status": "failed",
  "attempt_count": 1,
  "last_http_status": 503,
  "last_error": "Non-2xx response: HTTP 503",
  "created_at": "2026-09-13T09:41:32.418214",
  "updated_at": "2026-09-13T09:41:32.425102",
  "delivered_at": null,
  "attempts": [
    {
      "attempt_number": 1,
      "status": "failed",
      "destination_url": "http://receiver:8001/callback",
      "method": "POST",
      "request_headers": {
        "Content-Type": "application/json",
        "User-Agent": "callback-inspector/0.1",
        "X-Signature": "demo-signature",
        "X-Callback-Id": "336cb264c4e14d988bdd255dc5511383",
        "X-Callback-Attempt": "1"
      },
      "request_body": "{\"event\":\"payment.succeeded\",\"order_id\":\"ORD-1001\",\"amount\":4200,\"currency\":\"AED\"}",
      "http_status": 503,
      "response_headers": {"content-type": "application/json", "server": "uvicorn", "...": "..."},
      "response_body": "{\"error\":\"service unavailable (simulated)\"}",
      "error": "Non-2xx response: HTTP 503",
      "latency_ms": 2.91,
      "started_at": "2026-09-13T09:41:32.420640",
      "finished_at": "2026-09-13T09:41:32.424891"
    }
  ]
}
```

All endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/deliveries` | create a delivery and attempt it once |
| `GET` | `/api/deliveries?limit=&offset=` | list deliveries (newest first) |
| `GET` | `/api/deliveries/{id}` | delivery with all attempts |
| `GET` | `/api/deliveries/{id}/attempts` | attempt history only |
| `POST` | `/api/deliveries/{id}/retry` | manual retry (`409` unless status is `failed`) |

Every outbound request carries `X-Callback-Id` and `X-Callback-Attempt` headers, so the partner
can correlate on their side too.

### Delivery statuses

| Status | Meaning |
|---|---|
| `pending` | persisted, HTTP call not finished yet (also what you would see after a crash mid-send) |
| `delivered` | partner answered `2xx` |
| `failed` | non-`2xx` response, timeout, connection error, DNS failure… — `last_error` says which |

## Retry flow

```text
POST /api/deliveries/{id}/retry
  │
  ├─ status != failed ──────────────────► 409 Conflict (nothing is sent)
  │
  └─ status == failed
       ├─ insert DeliveryAttempt #N+1 (pending) + commit   ← durable before I/O
       ├─ httpx request to destination_url (same method/headers/body, same timeout)
       ├─ record http_status / response body / latency / error on attempt #N+1
       └─ delivery.status := attempt.status, delivery.attempt_count := N+1
```

Retry is deliberately **manual** in the MVP: the point of the tool is a human looking at the
evidence and deciding, not a background worker hammering a partner. The same primitive
(`service.send_attempt`) is what a scheduled/automatic retry would call later.

The web UI shows the **Retry now** button only on failed deliveries; it swaps the detail panel in
place via HTMX and the new attempt appears at the top of the history.

## Architecture

```text
app/
├── main.py           FastAPI app factory, lifespan (create tables), /health
├── api.py            JSON API (routers under /api)
├── web.py            Jinja2 + HTMX UI (/, /deliveries/{id}, HTMX partials)
├── service.py        the domain logic: create_delivery, send_attempt, retry_delivery
├── models.py         SQLAlchemy 2.0 models: CallbackDelivery 1—N DeliveryAttempt
├── schemas.py        Pydantic request/response models
├── database.py       engine / SessionLocal / get_db
├── http_client.py    httpx.AsyncClient dependency (overridden in tests)
├── config.py         env-based settings
├── mock_receiver.py  demo partner: answers 200/503, records what it received
├── static/           htmx.min.js vendored at build time by scripts/vendor_htmx.py (CDN fallback)
└── templates/        base, index, detail, partials/*
```

Data model:

```text
callback_deliveries                      delivery_attempts
────────────────────                     ─────────────────────
id (uuid hex)                            id
destination_url, method                  delivery_id  ─┐ FK
headers (json), payload (json)           attempt_number │
timeout_seconds                          destination_url, method
status  pending|delivered|failed         request_headers (json), request_body
attempt_count                            status  pending|delivered|failed
last_http_status, last_error             http_status, response_headers, response_body
created_at, updated_at, delivered_at     error, latency_ms
                                         started_at, finished_at
```

Key design decision — **persist before send.** `create_delivery` commits the delivery as
`pending`; `send_attempt` commits the attempt row as `pending` and only *then* performs the
network call. A crash during the call leaves a `pending` row with the full request, instead of a
delivery that vanished without trace. `tests/test_persist_before_send.py` asserts this by
observing the rows from a second DB session *inside* a fake transport.

Other choices:

* **SQLite via SQLAlchemy** — zero-ops for an MVP; `DATABASE_URL` accepts any SQLAlchemy URL.
* **httpx.AsyncClient** injected as a FastAPI dependency — tests route it into the mock receiver
  with `ASGITransport`, so the whole 503→retry→200 scenario runs in-process without sockets.
* **Server-rendered UI (Jinja2 + HTMX)** — no build step; the list auto-refreshes every 5 s.
* **Bodies are stored as text**, truncated at `MAX_STORED_BODY_CHARS` so a huge partner response
  cannot blow up the DB.

Not in the MVP (deliberately): automatic retries / backoff, authentication on the API,
signing of outbound payloads, multi-tenant partners, async fire-and-forget mode. Each of these
plugs into `service.py` without touching the storage model.

## Demo scenario

The scenario the project exists for, scripted in [`examples/demo.sh`](examples/demo.sh) and
covered by `tests/test_delivery_flow.py::test_503_then_retry_after_switching_to_200`:

```text
send callback
→ receiver returns 503
→ delivery marked failed
→ switch receiver to 200
→ retry
→ delivery marked delivered
```

Step by step with `docker compose up` running:

```bash
# 1. break the partner
curl -X POST http://localhost:8001/mode/503

# 2. send a callback -> "status": "failed", "last_http_status": 503
curl -X POST http://localhost:8000/api/deliveries -H 'Content-Type: application/json' \
     -d @examples/create_delivery.json
# open http://localhost:8000 — the delivery is red, details show the 503 response body

# 3. partner fixes their side
curl -X POST http://localhost:8001/mode/200

# 4. retry (from the UI button or the API) -> "status": "delivered", "attempt_count": 2
curl -X POST http://localhost:8000/api/deliveries/<id>/retry

# 5. history: attempt #1 failed/503, attempt #2 delivered/200, both with full request/response
curl http://localhost:8000/api/deliveries/<id>/attempts

# what the partner really received (both attempts, with X-Callback-Attempt: 1 and 2)
curl http://localhost:8001/received
```

`bash examples/demo.sh` does all of the above and fails loudly if any state is not what it
should be — CI runs it against the real `docker compose` stack after the unit tests.

Mock receiver endpoints: `POST /callback` (answers with the current mode), `GET|POST /mode/{200|503}`,
`GET /received`, `DELETE /received`, `GET /health`.

## Testing & linting

```bash
pytest -q                 # 11 tests: API flow, persist-before-send, web UI smoke
ruff check . && ruff format --check .
```

CI (`.github/workflows/ci.yml`): ruff + pytest on Python 3.11 and 3.12, then `docker compose build`
and the demo scenario against the running stack.

## License

MIT — see [LICENSE](LICENSE).
