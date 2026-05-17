# tc-execution-engine

A standalone Python FastAPI service that receives HMAC-signed order requests from the Target Capital app, places orders with Indian brokers (Dhan live; Zerodha/Angel/Upstox stubbed), writes fills to the shared Postgres database, and returns structured responses.

## Run & Operate

- `tc-execution-engine` workflow — runs `uvicorn` on port 5000 with hot reload
- `pnpm --filter @workspace/api-server run dev` — run the Node.js API server (port from env)
- Required env vars: see `artifacts/tc-execution-engine/.env.example`

## Stack

- **Engine**: Python 3.11, FastAPI, Uvicorn/Gunicorn
- **DB ORM**: SQLAlchemy 2.0, psycopg2-binary (Postgres)
- **Validation**: Pydantic v2
- **Crypto**: cryptography (Fernet) for broker credential encryption
- **Broker**: dhanhq SDK (Dhan live), stubs for Zerodha/Angel/Upstox
- **Caching**: cachetools TTLCache for idempotency
- **Deployment**: Procfile (Railway), railway.toml, scripts/deploy_ec2.sh (EC2 future)
- Node.js workspace: pnpm workspaces, Express 5, Drizzle ORM (separate from the engine)

## Where things live

```
artifacts/tc-execution-engine/
  main.py                    # Entry point (uvicorn/gunicorn)
  requirements.txt           # Python deps
  Procfile                   # Railway deployment
  railway.toml               # Railway config
  .env.example               # Env var template
  app/
    main.py                  # FastAPI app factory
    middleware/
      hmac_auth.py           # HMAC-SHA256 verification dependency
      idempotency.py         # 24h TTL LRU idempotency cache
    routers/
      health.py              # GET /healthz, GET /version, GET|PUT /v1/halt
      orders.py              # POST /v1/orders, POST /v1/orders/{id}/cancel, GET /v1/orders/{id}
      admin.py               # /admin/api/* — status, trades, halt, test-order (token-gated)
    static/
      index.html             # /admin dashboard (vanilla JS, dark theme)
  shared/
    db.py                    # DB connection + startup self-test
    models.py                # SQLAlchemy ORM models (User, BrokerAccount, Trade, BrokerOrder, TradingSignal)
    schemas.py               # Pydantic request/response schemas
    crypto.py                # Fernet encrypt/decrypt for broker credentials
    brokers/
      __init__.py            # Broker factory (get_executor)
      base.py                # BrokerExecutor abstract base
      dhan.py                # DhanExecutor (live, uses dhanhq SDK)
      stubs.py               # Zerodha/Angel/Upstox (raise NotImplementedError)
  tests/
    smoke.sh                 # End-to-end curl smoke test (signs HMAC, checks DB row)
  scripts/
    deploy_ec2.sh            # Future EC2 deployment script
  docs/
    architecture.md          # Architecture diagram + DB permissions + error taxonomy
```

## Architecture decisions

- HMAC-SHA256 over `timestamp + "." + raw_body` — same scheme as Target Capital. Timestamp window is ±60s.
- Idempotency via in-process `cachetools.TTLCache` (24h / 50k keys). Upgrade path to Redis: swap `cache.get/set` calls — nothing else changes.
- Halt state persisted in a SQLite file (`halt_state.db`) — survives pod restarts without needing Redis or extra DB writes.
- DB self-test at startup: SELECT 1 (must pass) + DELETE FROM users WHERE 1=0 (must fail with permission denied).
- Broker credentials stored encrypted with Fernet using `BROKER_MASTER_KEY` — same key as Target Capital.
- Error taxonomy: `auth_error` (401), `validation_error` (422), `broker_error` (502), `halted` (503), `not_found` (404).

## Admin UI

Browser dashboard at `/admin` so you don't have to curl from the shell.

- Open port 5000 from the Replit **Ports** panel, then navigate to `/admin`.
- Sign in with your `ADMIN_TOKEN` value (stored in sessionStorage for the tab only).
- Features: live engine/DB/config status, halt toggle with reason, recent trades table, and a "place test order" form that signs HMAC server-side and POSTs to `/v1/orders` (exercises the full pipeline end-to-end).
- All admin endpoints (`/admin/api/*`) require the `X-TC-Admin-Token` header.

## Product

tc-execution-engine is the order placement layer for Target Capital. It:
- Accepts HMAC-signed order requests from Target Capital
- Routes to the correct broker (Dhan in Phase 1)
- Enforces idempotency (no duplicate orders on retry)
- Supports an emergency halt switch (PUT /v1/halt)
- Writes trade records to the shared Postgres database

## Secrets required

Set these in Replit Secrets (or Railway env vars / AWS SSM for production):

| Key | Description |
|-----|-------------|
| `DATABASE_URL` | PostgreSQL DSN for tc_exec scoped user |
| `EXECUTION_HMAC_SECRET` | Shared HMAC secret with Target Capital |
| `BROKER_MASTER_KEY` | Fernet key for broker credential encryption |
| `ADMIN_TOKEN` | Protects PUT /v1/halt |

## User preferences

- Python 3.11 FastAPI service
- Port 5000 for Replit dev, reads `$PORT` for Railway/EC2
- Deploy to Railway first, EC2 (static Elastic IP) later
- Dhan broker live in Phase 1; Zerodha/Angel/Upstox stubbed

## Gotchas

- `BROKER_MASTER_KEY` must be a valid Fernet key (base64-encoded 32 bytes). Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `EXECUTION_HMAC_SECRET` must match the value in Target Capital exactly.
- The tc_exec Postgres user must have: READ on users/broker_account/trading_signal; INSERT+UPDATE on trade/broker_order. The startup self-test verifies this.
- Smoke test: `bash artifacts/tc-execution-engine/tests/smoke.sh` (requires `EXECUTION_HMAC_SECRET` in env and real user/broker_account UUIDs).

## Pointers

- See `artifacts/tc-execution-engine/docs/architecture.md` for the full request flow diagram and DB permissions table.
- See the `pnpm-workspace` skill for the Node.js workspace structure.
