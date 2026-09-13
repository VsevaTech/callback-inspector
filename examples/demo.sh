#!/usr/bin/env bash
# End-to-end demo of the core scenario against a running `docker compose up`:
#
#   send callback -> receiver returns 503 -> delivery marked failed
#   -> switch receiver to 200 -> retry -> delivery marked delivered
#
# Exits non-zero if any step does not produce the expected state (CI uses it as a smoke test).
set -euo pipefail

INSPECTOR=${INSPECTOR_URL:-http://localhost:8000}
RECEIVER=${RECEIVER_URL:-http://localhost:8001}
# URL the *inspector container* uses to reach the receiver (compose service name by default).
DESTINATION=${DESTINATION_URL:-http://receiver:8001/callback}
HERE=$(cd "$(dirname "$0")" && pwd)

field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
step()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "1. Break the partner: receiver answers 503"
curl -fsS -X POST "$RECEIVER/mode/503"; echo

step "2. Send a callback through Callback Inspector"
BODY=$(python3 -c '
import json, sys
body = json.load(open(sys.argv[1])); body["destination_url"] = sys.argv[2]; print(json.dumps(body))
' "$HERE/create_delivery.json" "$DESTINATION")
CREATE=$(curl -fsS -X POST "$INSPECTOR/api/deliveries" -H 'Content-Type: application/json' -d "$BODY")
echo "$CREATE" | python3 -m json.tool
ID=$(echo "$CREATE" | field id)
STATUS=$(echo "$CREATE" | field status)
HTTP=$(echo "$CREATE" | field last_http_status)
echo "delivery=$ID status=$STATUS http=$HTTP"
[[ "$STATUS" == "failed" && "$HTTP" == "503" ]] || { echo "FAIL: expected failed/503"; exit 1; }

step "3. Partner fixes their side: receiver answers 200"
curl -fsS -X POST "$RECEIVER/mode/200"; echo

step "4. Manual retry"
RETRY=$(curl -fsS -X POST "$INSPECTOR/api/deliveries/$ID/retry")
echo "$RETRY" | python3 -m json.tool
STATUS=$(echo "$RETRY" | field status)
ATTEMPTS=$(echo "$RETRY" | field attempt_count)
echo "status=$STATUS attempts=$ATTEMPTS"
[[ "$STATUS" == "delivered" && "$ATTEMPTS" == "2" ]] || { echo "FAIL: expected delivered after 2 attempts"; exit 1; }

step "5. Full attempt history"
curl -fsS "$INSPECTOR/api/deliveries/$ID/attempts" | python3 -c '
import json, sys
for a in json.load(sys.stdin):
    print("  #{attempt_number}  {status:<10} HTTP {http_status}  {latency_ms} ms  {started_at}".format(**a))
'

step "6. What the partner actually received"
echo "  receiver got $(curl -fsS "$RECEIVER/received" | field count) requests"

printf '\n\033[1;32mOK — open %s/deliveries/%s to see it in the UI\033[0m\n' "$INSPECTOR" "$ID"
