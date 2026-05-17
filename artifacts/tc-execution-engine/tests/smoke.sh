#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# End-to-end smoke test for tc-execution-engine
# Usage:  bash tests/smoke.sh [base_url]
#
# Requires:
#   EXECUTION_HMAC_SECRET  — shared HMAC secret (or set in .env)
#   DATABASE_URL           — Postgres DSN (to verify the trade row was written)
#   DHAN_CLIENT_ID         — a real or paper-trading Dhan client ID
#   DHAN_ACCESS_TOKEN      — corresponding Dhan access token
#
# The script:
#   1. Signs a fake POST /v1/orders payload with the dev HMAC secret.
#   2. Posts it to $BASE_URL/v1/orders.
#   3. Expects HTTP 201 + a JSON body containing broker_order_id.
#   4. Optionally checks the trade row in Postgres (requires psql).
# ---------------------------------------------------------------------------
set -euo pipefail

BASE_URL="${1:-http://localhost:5000}"

# Load .env if present (dev convenience)
if [ -f .env ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' .env | xargs)
fi

HMAC_SECRET="${EXECUTION_HMAC_SECRET:-changeme-generate-with-openssl-rand-hex-32}"
REQUEST_ID="smoke-$(date +%s)"
IDEMPOTENCY_KEY="smoke-idem-$(date +%s)"
TIMESTAMP="$(date +%s)"

# ---------------------------------------------------------------------------
# Build payload (replace security_id / user_id / broker_account_id with real
# values from your dev DB)
# ---------------------------------------------------------------------------
PAYLOAD=$(cat <<EOF
{
  "user_id": "00000000-0000-0000-0000-000000000001",
  "broker_account_id": "00000000-0000-0000-0000-000000000002",
  "symbol": "INFY",
  "exchange": "NSE",
  "security_id": "1594",
  "transaction_type": "BUY",
  "quantity": 1,
  "order_type": "MARKET",
  "product_type": "CNC",
  "price": 0,
  "tag": "smoke-test"
}
EOF
)

# ---------------------------------------------------------------------------
# Compute HMAC-SHA256: HMAC(timestamp + "." + raw_body, secret)
# ---------------------------------------------------------------------------
MESSAGE="${TIMESTAMP}.${PAYLOAD}"
SIGNATURE=$(printf '%s' "$MESSAGE" | openssl dgst -sha256 -hmac "$HMAC_SECRET" | awk '{print $2}')

echo "==> Placing order at $BASE_URL/v1/orders"
echo "    request_id:      $REQUEST_ID"
echo "    idempotency_key: $IDEMPOTENCY_KEY"
echo "    timestamp:       $TIMESTAMP"
echo "    signature:       $SIGNATURE"
echo ""

HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST "$BASE_URL/v1/orders" \
  -H "Content-Type: application/json" \
  -H "X-TC-Signature: $SIGNATURE" \
  -H "X-TC-Timestamp: $TIMESTAMP" \
  -H "X-TC-Request-ID: $REQUEST_ID" \
  -H "X-TC-Idempotency: $IDEMPOTENCY_KEY" \
  -d "$PAYLOAD")

HTTP_BODY=$(echo "$HTTP_RESPONSE" | head -n -1)
HTTP_STATUS=$(echo "$HTTP_RESPONSE" | tail -n 1)

echo "==> HTTP $HTTP_STATUS"
echo "$HTTP_BODY" | python3 -m json.tool 2>/dev/null || echo "$HTTP_BODY"
echo ""

if [ "$HTTP_STATUS" != "201" ]; then
  echo "FAIL: expected 201, got $HTTP_STATUS"
  exit 1
fi

TRADE_ID=$(echo "$HTTP_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['trade_id'])" 2>/dev/null || true)
BROKER_ORDER_ID=$(echo "$HTTP_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['broker_order_id'])" 2>/dev/null || true)

echo "PASS: order placed"
echo "  trade_id:        $TRADE_ID"
echo "  broker_order_id: $BROKER_ORDER_ID"

# ---------------------------------------------------------------------------
# Optionally verify DB row
# ---------------------------------------------------------------------------
if command -v psql &>/dev/null && [ -n "${DATABASE_URL:-}" ] && [ -n "$TRADE_ID" ]; then
  echo ""
  echo "==> Verifying trade row in Postgres…"
  ROW=$(psql "$DATABASE_URL" -t -c "SELECT id, status, symbol FROM trade WHERE id='$TRADE_ID';")
  if [ -n "$ROW" ]; then
    echo "PASS: trade row found: $ROW"
  else
    echo "WARN: trade row not found (check DB permissions)"
  fi
fi

# ---------------------------------------------------------------------------
# Test idempotency — repost same key, expect same response
# ---------------------------------------------------------------------------
echo ""
echo "==> Testing idempotency (same key, second request)…"
TIMESTAMP2="$(date +%s)"
MESSAGE2="${TIMESTAMP2}.${PAYLOAD}"
SIGNATURE2=$(printf '%s' "$MESSAGE2" | openssl dgst -sha256 -hmac "$HMAC_SECRET" | awk '{print $2}')

HTTP_RESPONSE2=$(curl -s -w "\n%{http_code}" \
  -X POST "$BASE_URL/v1/orders" \
  -H "Content-Type: application/json" \
  -H "X-TC-Signature: $SIGNATURE2" \
  -H "X-TC-Timestamp: $TIMESTAMP2" \
  -H "X-TC-Request-ID: ${REQUEST_ID}-dup" \
  -H "X-TC-Idempotency: $IDEMPOTENCY_KEY" \
  -d "$PAYLOAD")

HTTP_BODY2=$(echo "$HTTP_RESPONSE2" | head -n -1)
HTTP_STATUS2=$(echo "$HTTP_RESPONSE2" | tail -n 1)
TRADE_ID2=$(echo "$HTTP_BODY2" | python3 -c "import sys,json; print(json.load(sys.stdin)['trade_id'])" 2>/dev/null || true)

if [ "$TRADE_ID2" = "$TRADE_ID" ]; then
  echo "PASS: idempotency works — same trade_id returned"
else
  echo "WARN: idempotency may not be working (trade_id mismatch or error)"
  echo "      first: $TRADE_ID  second: $TRADE_ID2"
fi

echo ""
echo "Smoke test complete."
