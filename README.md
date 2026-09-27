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

## Security & delivery guarantees

```text
Client
  │
  │ Authorization + Idempotency-Key
  ▼
Callback Inspector
  │
  ├─ persist sanitized evidence      (Authorization → ***REDACTED***)
  │
  ├─ enforce idempotency             (same key + same request → existing delivery)
  │
  └─ HMAC-sign callback              (X-Callback-Timestamp + X-Callback-Signature)
             │
             ▼  original Authorization, signed body
          Partner
```

**Persist before send.** The delivery and every attempt are committed as `pending` *before* any
network I/O, so a crash mid-send leaves a record instead of silence.

**Sensitive-header redaction — secrets are delivered but not retained as evidence.** Header values
for `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, `X-Auth-Token`,
`X-Access-Token`, `X-Internal-Token` (case-insensitive) are sent to the partner unchanged, and
replaced by `***REDACTED***` in everything that is stored or shown: SQLite, REST API, web UI,
attempt history — for request *and* partner response headers. `SENSITIVE_HEADERS=X-Partner-Secret,...`
**extends** the list; an empty value never disables the defaults.

```text
incoming delivery request ─► original headers ─► outbound HTTP request   (partner gets the real token)
                             original headers ─► redaction ─► stored evidence
```

Original sensitive values are kept **only in process memory** so that a manual retry can resend them.
After a restart they are gone; retry then answers `409 SENSITIVE_HEADERS_UNAVAILABLE` (nothing is sent)
until the caller re-supplies them: `POST /api/deliveries/{id}/retry` with
`{"headers": {"Authorization": "Bearer ..."}}` — used for sending, never stored.

**HMAC signing — optional HMAC-SHA256 signing proves callback authenticity.** With
`CALLBACK_SIGNING_ENABLED=true` and `CALLBACK_SIGNING_SECRET=...` every attempt carries:

```text
X-Callback-Timestamp: 1790503200                         # UTC Unix seconds, fresh per attempt
X-Callback-Signature: sha256=<hex>                       # lower-case hex
<hex> = HMAC_SHA256(secret, timestamp + "." + raw_body)  # raw_body = the exact bytes sent
```

The payload is serialised once; those bytes are signed and sent. Caller-supplied
`X-Callback-Signature` / `X-Callback-Timestamp` headers are **overwritten** (never trusted) when signing
is on. Each manual retry gets a new timestamp and signature. Verification on the partner side:
recompute over the raw body, compare with `hmac.compare_digest`, reject timestamps outside ±5 min —
see `app/signing.py::verify_signature` and the mock receiver. The app refuses to start
(`SIGNING_CONFIGURATION_ERROR`) with signing enabled and an empty secret.

**Idempotent creation — `Idempotency-Key` prevents accidental duplicate callback creation after
client retries/timeouts.**

| `POST /api/deliveries` | Result |
|---|---|
| no `Idempotency-Key` | unchanged behaviour: new delivery, `201` |
| new key | delivery created and sent, `201` |
| same key + same logical request | existing delivery, **no outbound request**, `200`, `"idempotent_replay": true`, `Idempotent-Replayed: true` |
| same key + different request | `409 {"code": "IDEMPOTENCY_CONFLICT", ...}`, nothing sent |
| invalid key (empty, >255, control/non-ASCII chars) | `400 INVALID_IDEMPOTENCY_KEY` |

The "logical request" is SHA-256 over canonical JSON (sorted keys) of `destination_url`, `method`,
`headers` (names lower-cased; generated `X-Callback-Id/-Attempt/-Signature/-Timestamp` excluded;
sensitive values replaced by the redaction marker, so no secret-derived hash is stored — a rotated
token is the same request), `payload`, `timeout_seconds`. The key → delivery mapping lives in
`callback_deliveries` (`idempotency_key` + `request_fingerprint`, **unique index**), so it survives
restarts, and the unique index settles concurrent duplicates (the loser of the race gets the replay).
Manual retry is *not* idempotent creation: it always adds attempt `#N+1` to the existing delivery.

Idempotency protects duplicate delivery creation for repeated requests using the same key. It is
**not** exactly-once delivery: a partner can still receive an attempt twice (e.g. it processed a
request but timed out answering, and someone retried).

**Security boundaries.** Callback Inspector is not a secret manager. The signing secret exists only
in the runtime environment (never in SQLite, API, UI or logs; excluded from `repr`). Partner
credentials arrive with each delivery request, are used for sending, and are redacted from evidence.
Logs contain identifiers and outcomes only — never headers or bodies.

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

| Variable | Default | Meaning |
|---|---|---|
| `SENSITIVE_HEADERS` | *(empty)* | extra header names to redact (extends the built-in list) |
| `CALLBACK_SIGNING_ENABLED` | `false` | HMAC-SHA256 sign outbound callbacks |
| `CALLBACK_SIGNING_SECRET` | *(empty)* | signing secret — environment only; required when signing is on |
| `CALLBACK_SIGNATURE_HEADER` | `X-Callback-Signature` | signature header name |
| `CALLBACK_TIMESTAMP_HEADER` | `X-Callback-Timestamp` | timestamp header name |
| `SIGNATURE_SECRET` *(receiver)* | *(empty)* | mock receiver verifies signatures when set (compose: `RECEIVER_SIGNATURE_SECRET`) |

## API example

Create a delivery (persist → send once → return the result):

```bash
curl -X POST http://localhost:8000/api/deliveries \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: payment-ORD-1001-succeeded' \
  -d '{
    "destination_url": "http://receiver:8001/callback",
    "method": "POST",
    "headers": {"X-Signature": "demo-signature", "Authorization": "Bearer DEMO_SECRET"},
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
  "headers": {"X-Signature": "demo-signature", "Authorization": "***REDACTED***"},
  "payload": {"event": "payment.succeeded", "order_id": "ORD-1001", "amount": 4200, "currency": "AED"},
  "timeout_seconds": 5.0,
  "idempotency_key": "payment-ORD-1001-succeeded",
  "request_fingerprint": "9c1e…",
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
        "User-Agent": "callback-inspector/0.2",
        "X-Signature": "demo-signature",
        "Authorization": "***REDACTED***",
        "X-Callback-Id": "336cb264c4e14d988bdd255dc5511383",
        "X-Callback-Attempt": "1"
      },
      "request_body": "{\"event\":\"payment.succeeded\",\"order_id\":\"ORD-1001\",\"amount\":4200,\"currency\":\"AED\"}",
      "http_status": 503,
      "response_headers": {"content-type": "application/json", "server": "uvicorn", "...": "..."},
      "response_body": "{\"error\":\"service unavailable (simulated)\"}",
      "error": "Non-2xx response: HTTP 503",
      "latency_ms": 2.91,
      "signature_algorithm": null,
      "signature_timestamp": null,
      "started_at": "2026-09-13T09:41:32.420640",
      "finished_at": "2026-09-13T09:41:32.424891"
    }
  ],
  "idempotent_replay": false
}
```

All endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/deliveries` | create a delivery and attempt it once (optional `Idempotency-Key`) |
| `GET` | `/api/deliveries?limit=&offset=` | list deliveries (newest first) |
| `GET` | `/api/deliveries/{id}` | delivery with all attempts |
| `GET` | `/api/deliveries/{id}/attempts` | attempt history only |
| `POST` | `/api/deliveries/{id}/retry` | manual retry (`409` unless status is `failed`); optional body `{"headers": {...}}` re-supplies redacted credentials |

Structured errors: `{"code": "...", "message": "...", "detail": "..."}` — `IDEMPOTENCY_CONFLICT` (409),
`INVALID_IDEMPOTENCY_KEY` (400), `SENSITIVE_HEADERS_UNAVAILABLE` (409), `INVALID_RETRY_HEADERS` (422);
`SIGNING_CONFIGURATION_ERROR` is raised at startup.

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
       ├─ restore redacted credentials (memory or request body), fresh timestamp + HMAC if signing
       ├─ httpx request to destination_url (same method/headers/body, same timeout)
       ├─ record http_status / response body / latency / error on attempt #N+1
       └─ delivery.status := attempt.status, delivery.attempt_count := N+1
```

Retry is deliberately **manual** in the MVP: the point of the tool is a human looking at the
evidence and deciding, not a background worker hammering a partner. The same primitive
(`service.send_attempt`) is what a scheduled/automatic retry would call later.

The detail page has a **Security** panel (which headers were redacted, signing mode, idempotency key
— HTML-escaped and shortened) and each attempt shows `Signature: generated (HMAC-SHA256) · Timestamp`.
The web UI shows the **Retry now** button only on failed deliveries; it swaps the detail panel in
place via HTMX and the new attempt appears at the top of the history.

## Architecture

```text
app/
├── main.py           FastAPI app factory, lifespan (create tables), /health
├── api.py            JSON API (routers under /api)
├── web.py            Jinja2 + HTMX UI (/, /deliveries/{id}, HTMX partials)
├── service.py        the domain logic: create_delivery, create_or_get_delivery, send_attempt, retry_delivery
├── security.py       header redaction, Idempotency-Key validation, request fingerprint
├── signing.py        HMAC-SHA256 sign/verify contract (shared with the mock receiver)
├── migrations.py     minimal in-place SQLite migrations (schema_migrations table)
├── errors.py         structured {"code","message"} API errors
├── models.py         SQLAlchemy 2.0 models: CallbackDelivery 1—N DeliveryAttempt
├── schemas.py        Pydantic request/response models
├── database.py       engine / SessionLocal / get_db
├── http_client.py    httpx.AsyncClient dependency (overridden in tests)
├── config.py         env-based settings
├── mock_receiver.py  demo partner: answers 200/503, records what it received, optional HMAC verification
├── static/           htmx.min.js vendored at build time by scripts/vendor_htmx.py (CDN fallback)
└── templates/        base, index, detail, partials/*
```

Data model:

```text
callback_deliveries                      delivery_attempts
────────────────────                     ─────────────────────
id (uuid hex)                            id
destination_url, method                  delivery_id  ─┐ FK
headers (json, redacted), payload       attempt_number │
timeout_seconds                          destination_url, method
idempotency_key (unique), fingerprint    request_headers (json, redacted), request_body
status  pending|delivered|failed         status  pending|delivered|failed
attempt_count                            http_status, response_headers (redacted), response_body
last_http_status, last_error             error, latency_ms
created_at, updated_at, delivered_at     signature_algorithm, signature_timestamp
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
* **Migrations without Alembic** — `init_db` runs `create_all` and then `app/migrations.py`: idempotent
  steps recorded in `schema_migrations`. Upgrading an existing `callback_inspector.db` adds the new
  columns + unique index, **redacts sensitive headers already stored by older versions** and
  `VACUUM`s the file so old values do not linger in free pages. No manual step needed.

## Known limitations

* **Not exactly-once.** Idempotency protects duplicate delivery *creation* for repeated requests using
  the same key; the partner should still de-duplicate on `X-Callback-Id`.
* **Redacted credentials are not durable.** They live in process memory for manual retry; after a
  restart (or with several worker processes) the caller must re-supply them on retry. Old evidence
  redacted by the migration can only be retried with re-supplied credentials.
* **Redaction is by header name.** Secrets placed in the URL query string or in the payload are stored
  as-is — put credentials in headers (or add their header names to `SENSITIVE_HEADERS`).
* **One signing secret for all partners**, no key rotation window / multiple active secrets yet.
* **Replays while the first request is still in flight** return the existing delivery in `pending` state.
* **SQLite MVP**: single-node; the unique index is the race guard.
* Not in scope (deliberately): automatic retries / backoff, authentication on the API, multi-tenant
  partners, async fire-and-forget mode, queues. Each of these plugs into `service.py`.

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

### Secure delivery demo

[`examples/demo_secure_delivery.sh`](examples/demo_secure_delivery.sh) needs signing turned on — the
overlay [`docker-compose.secure-demo.yml`](docker-compose.secure-demo.yml) sets the same **synthetic**
secret in the inspector and in the receiver (which then rejects unsigned/invalid callbacks with `401`):

```bash
docker compose -f docker-compose.yml -f docker-compose.secure-demo.yml up --build -d
bash examples/demo.sh                    # 503 → retry → 200 still works, now signed
bash examples/demo_secure_delivery.sh
```

```text
1. stack up: inspector signing=true, receiver verifying
2. POST with Authorization: Bearer DEMO_SECRET, X-API-Key: partner-secret,
   Idempotency-Key: payment-ORD-1001-succeeded-<run>   → 201, delivered
3. receiver: Authorization/X-API-Key arrived intact, signature_valid=true
4. inspector API + UI: Authorization/X-API-Key = ***REDACTED***, signing HMAC-SHA256
5. identical POST, same key                             → 200, same id, idempotent_replay=true
6. same key, amount 4300                                → 409 IDEMPOTENCY_CONFLICT
7. receiver got exactly one callback
8. SQLite file scanned inside the container: DEMO_SECRET / partner-secret → 0 occurrences
```

The run id suffix keeps the demo re-runnable against the same database.

Mock receiver endpoints: `POST /callback` (answers with the current mode; with `SIGNATURE_SECRET` set it
first verifies the HMAC and answers `401 {"signature_valid": false, "reason": ...}` on failure),
`GET|POST /mode/{200|503}`, `GET /received` (includes `signature_valid`), `DELETE /received`, `GET /health`.

## Testing & linting

```bash
pytest -q                 # API flow, persist-before-send, web UI, redaction, signing, idempotency, migrations
ruff check . && ruff format --check .
```

CI (`.github/workflows/ci.yml`): ruff + pytest on Python 3.11 and 3.12, then `docker compose build`,
the demo scenario against the running stack, and both demos again with the secure overlay (HMAC valid,
secrets redacted incl. a raw scan of the SQLite file, idempotent replay does not redeliver, conflicting
replay → 409).

## License

MIT — see [LICENSE](LICENSE).
