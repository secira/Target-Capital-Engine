# tc-execution-engine — Architecture Notes

## Overview

`tc-execution-engine` is a standalone FastAPI service that:
1. Receives HMAC-signed order requests from the Target Capital app.
2. Validates the request (signature, timestamp, idempotency).
3. Places the order with the chosen Indian broker (Dhan live; others stubbed).
4. Writes the fill back to the shared Postgres database.
5. Returns a structured JSON response.

## Request Flow

```
Target Capital App
      │
      │  POST /v1/orders
      │  X-TC-Signature: <hmac>
      │  X-TC-Timestamp: <unix_ts>
      │  X-TC-Idempotency: <key>
      ▼
┌─────────────────────┐
│  HMAC Middleware     │  verify_hmac dependency
│  - timestamp ±60s   │  → 401 on failure
│  - HMAC-SHA256 check│
└────────┬────────────┘
         │
┌────────▼────────────┐
│  Idempotency Cache  │  in-process TTLCache (24h)
│  - LRU, 50k keys    │  → return cached response if duplicate
└────────┬────────────┘
         │
┌────────▼────────────┐
│  Halt Check          │  is_halted() reads SQLite
│                      │  → 503 {"error":"halted"} if halted
└────────┬────────────┘
         │
┌────────▼────────────┐
│  Order Router        │
│  - fetch BrokerAcct  │
│  - decrypt creds     │
│  - call executor     │
│  - write Trade/BO    │
└────────┬────────────┘
         │
┌────────▼────────────┐
│  BrokerExecutor      │
│  (Dhan / stubs)      │
└─────────────────────┘
```

## Database Permissions (tc_exec user)

| Table           | Read | Insert | Update | Delete |
|-----------------|------|--------|--------|--------|
| users           | ✓    |        |        |        |
| broker_account  | ✓    |        |        |        |
| trading_signal  | ✓    |        |        |        |
| trade           | ✓    | ✓      | ✓      |        |
| broker_order    | ✓    | ✓      | ✓      |        |

The startup self-test verifies this by attempting `DELETE FROM users LIMIT 1`,
which MUST fail with "permission denied".

## HMAC Protocol

```
message  = timestamp_unix_seconds + "." + raw_request_body_bytes
signature = HMAC-SHA256(message, EXECUTION_HMAC_SECRET).hex()
```

Headers required on every mutable endpoint:
- `X-TC-Signature` — hex digest
- `X-TC-Timestamp` — Unix epoch seconds (string)
- `X-TC-Request-ID` — UUID string (optional, recommended)
- `X-TC-Idempotency` — idempotency key (optional)

## Idempotency

- In-process `cachetools.TTLCache` — 24h TTL, 50k key cap.
- On a cache miss, also checks the `trade.idempotency_key` column (survives restarts).
- **Upgrade path to Redis**: replace `IdempotencyCache.get` / `.set` with
  `redis.get` / `redis.setex(key, 86400, json_bytes)`. No other changes needed.

## Halt Switch

- `PUT /v1/halt` with `X-TC-Admin-Token` toggles the engine on/off.
- State stored in a SQLite file (`halt_state.db`) — survives pod restarts.
- When halted, `POST /v1/orders` returns `{"error":"halted"}` (HTTP 503).

## Broker Adapters

| Broker   | Status  | Notes                              |
|----------|---------|------------------------------------|
| Dhan     | Live    | Uses `dhanhq` SDK                  |
| Zerodha  | Stubbed | Raises `NotImplementedError`       |
| Angel    | Stubbed | Raises `NotImplementedError`       |
| Upstox   | Stubbed | Raises `NotImplementedError`       |

## Error Taxonomy

| Code              | HTTP | Meaning                                 |
|-------------------|------|-----------------------------------------|
| `auth_error`      | 401  | Bad/missing HMAC signature or token     |
| `validation_error`| 422  | Invalid request payload                 |
| `broker_error`    | 502  | Broker API returned an error            |
| `halted`          | 503  | Engine is halted                        |
| `not_found`       | 404  | Trade/resource not found                |

## Deployment Targets

1. **Replit (dev)** — port 5000, `uvicorn main:app --reload --port 5000`
2. **Railway (staging/prod)** — reads `$PORT` from env, `Procfile` runs gunicorn
3. **AWS EC2 (future)** — static Elastic IP for broker IP-whitelisting; systemd
   service; secrets from AWS SSM Parameter Store; see `scripts/deploy_ec2.sh`
