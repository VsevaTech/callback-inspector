#!/usr/bin/env bash
# Secure delivery demo: sensitive-header redaction + HMAC-SHA256 signing + Idempotency-Key.
#
# Needs the stack started WITH signing (synthetic demo secret, see docker-compose.secure-demo.yml):
#
#   docker compose -f docker-compose.yml -f docker-compose.secure-demo.yml up --build -d
#   bash examples/demo_secure_delivery.sh
#
# Exits non-zero if any guarantee does not hold (CI uses it as a smoke test).
set -euo pipefail

INSPECTOR=${INSPECTOR_URL:-http://localhost:8000}
RECEIVER=${RECEIVER_URL:-http://localhost:8001}
DESTINATION=${DESTINATION_URL:-http://receiver:8001/callback}
# Container holding the SQLite file; set DB_CHECK=skip to skip the direct database scan.
INSPECTOR_CONTAINER=${INSPECTOR_CONTAINER:-callback-inspector}
DB_CHECK=${DB_CHECK:-docker}
# Unique per run so the demo can be re-run against the same database.
RUN_ID=${RUN_ID:-$(date +%s)}
KEY="payment-ORD-1001-succeeded-$RUN_ID"

# Synthetic demo credentials — they must reach the partner and must never be persisted.
AUTH_SECRET="DEMO_SECRET"
API_KEY_SECRET="partner-secret"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31mFAIL: %s\033[0m\n' "$*"; exit 1; }
ok()   { printf '  \033[32m✔ %s\033[0m\n' "$*"; }
py()   { python3 -c "$1"; }

body() {  # $1 = amount
  python3 - "$DESTINATION" "$1" <<'EOF'
import json, sys
print(json.dumps({
    "destination_url": sys.argv[1],
    "method": "POST",
    "headers": {
        "Authorization": "Bearer DEMO_SECRET",
        "X-API-Key": "partner-secret",
        "X-Partner-Id": "acme",
    },
    "payload": {"event": "payment.succeeded", "order_id": "ORD-1001", "amount": int(sys.argv[2]), "currency": "AED"},
    "timeout_seconds": 5,
}))
EOF
}

post() {  # $1 = amount → prints "<http_code>\n<body>"
  curl -sS -o /tmp/secure_demo_resp.json -w '%{http_code}' -X POST "$INSPECTOR/api/deliveries" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $KEY" -d "$(body "$1")"
}

step "1. Stack is up with signing enabled and the receiver verifying signatures"
HEALTH=$(curl -fsS "$INSPECTOR/health"); RHEALTH=$(curl -fsS "$RECEIVER/health")
echo "  inspector: $HEALTH"; echo "  receiver:  $RHEALTH"
echo "$HEALTH" | py 'import json,sys; sys.exit(0 if json.load(sys.stdin)["signing"] else 1)' \
  || fail "signing is disabled — start with -f docker-compose.secure-demo.yml"
echo "$RHEALTH" | py 'import json,sys; sys.exit(0 if json.load(sys.stdin)["signature_verification"] else 1)' \
  || fail "receiver does not verify signatures — start with -f docker-compose.secure-demo.yml"
curl -fsS -X POST "$RECEIVER/mode/200" >/dev/null
curl -fsS -X DELETE "$RECEIVER/received" >/dev/null
ok "receiver inbox cleared, mode 200"

step "2. Create delivery (Authorization + X-API-Key + Idempotency-Key: $KEY)"
CODE=$(post 4200); CREATE=$(cat /tmp/secure_demo_resp.json)
echo "$CREATE" | python3 -m json.tool
[[ "$CODE" == "201" ]] || fail "expected 201 Created, got $CODE"
ID=$(echo "$CREATE" | py 'import json,sys; print(json.load(sys.stdin)["id"])')
STATUS=$(echo "$CREATE" | py 'import json,sys; print(json.load(sys.stdin)["status"])')
[[ "$STATUS" == "delivered" ]] || fail "expected delivered, got $STATUS"
ok "201 Created, delivery $ID delivered"

step "3. Receiver verified the original credentials and the HMAC signature"
curl -fsS "$RECEIVER/received" | python3 -c '
import json, sys
items = json.load(sys.stdin)["items"]
assert len(items) == 1, f"expected 1 callback, got {len(items)}"
r = items[0]; h = r["headers"]
assert h["authorization"] == "Bearer DEMO_SECRET", "Authorization did not arrive intact"
assert h["x-api-key"] == "partner-secret", "X-API-Key did not arrive intact"
assert r["signature_valid"] is True, f"signature invalid: {r['signature_reason']}"
print("  Authorization :", h["authorization"])
print("  X-API-Key     :", h["x-api-key"])
print("  Timestamp     :", h["x-callback-timestamp"])
print("  Signature     :", h["x-callback-signature"])
print("  signature_valid:", r["signature_valid"])
' || fail "receiver-side verification"
ok "original credentials arrived, HMAC valid"

step "4. Inspector evidence is redacted"
curl -fsS "$INSPECTOR/api/deliveries/$ID" | python3 -c '
import json, sys
d = json.load(sys.stdin); a = d["attempts"][0]
R = "***REDACTED***"
assert d["headers"]["Authorization"] == R and d["headers"]["X-API-Key"] == R, d["headers"]
assert a["request_headers"]["Authorization"] == R and a["request_headers"]["X-API-Key"] == R
assert d["headers"]["X-Partner-Id"] == "acme"
assert a["signature_algorithm"] == "HMAC-SHA256"
raw = json.dumps(d)
assert "DEMO_SECRET" not in raw and "partner-secret" not in raw
print("  Authorization:", d["headers"]["Authorization"])
print("  X-API-Key    :", d["headers"]["X-API-Key"])
print("  X-Partner-Id :", d["headers"]["X-Partner-Id"])
print("  signing      :", a["signature_algorithm"], "ts", a["signature_timestamp"])
' || fail "API evidence not redacted"
PAGE=$(curl -fsS "$INSPECTOR/deliveries/$ID")
grep -q '\*\*\*REDACTED\*\*\*' <<<"$PAGE" || fail "UI does not show redaction"
grep -q 'HMAC-SHA256' <<<"$PAGE" || fail "UI does not show signing"
if grep -qE "$AUTH_SECRET|$API_KEY_SECRET" <<<"$PAGE"; then fail "UI leaks a secret"; fi
ok "API + UI show ***REDACTED***, signing HMAC-SHA256"

step "5. Identical replay with the same Idempotency-Key"
CODE=$(post 4200); REPLAY=$(cat /tmp/secure_demo_resp.json)
[[ "$CODE" == "200" ]] || fail "expected 200 replay, got $CODE"
echo "$REPLAY" | py "import json,sys; d=json.load(sys.stdin); assert d['id']=='$ID' and d['idempotent_replay'] is True and d['attempt_count']==1, d" \
  || fail "replay did not return the existing delivery"
ok "200, same delivery $ID, idempotent_replay=true"

step "6. Same key, amount 4300"
CODE=$(post 4300); CONFLICT=$(cat /tmp/secure_demo_resp.json)
echo "  $CONFLICT"
[[ "$CODE" == "409" ]] || fail "expected 409, got $CODE"
echo "$CONFLICT" | py 'import json,sys; assert json.load(sys.stdin)["code"]=="IDEMPOTENCY_CONFLICT"' || fail "wrong error code"
ok "409 IDEMPOTENCY_CONFLICT"

step "7. Receiver received exactly one callback"
COUNT=$(curl -fsS "$RECEIVER/received" | py 'import json,sys; print(json.load(sys.stdin)["count"])')
[[ "$COUNT" == "1" ]] || fail "receiver got $COUNT callbacks"
ok "exactly 1 outbound request"

step "8. SQLite: demo secrets physically absent"
if [[ "$DB_CHECK" == "docker" ]]; then
  docker exec -i "$INSPECTOR_CONTAINER" python - <<'EOF' || fail "secret found in the database"
import glob, sys
hits = 0
paths = glob.glob("/srv/data/callback_inspector.db*")
if not paths:
    sys.exit("no database file found under /srv/data")
for path in paths:
    data = open(path, "rb").read()
    for needle in (b"DEMO_SECRET", b"partner-secret"):
        n = data.count(needle)
        hits += n
        print(f"  {path}: {needle.decode()} -> {n} occurrences")
sys.exit(1 if hits else 0)
EOF
  ok "0 persisted occurrences"
else
  echo "  (skipped: DB_CHECK=$DB_CHECK)"
fi

printf '\n\033[1;32mOK — open %s/deliveries/%s to see the Security panel\033[0m\n' "$INSPECTOR" "$ID"
