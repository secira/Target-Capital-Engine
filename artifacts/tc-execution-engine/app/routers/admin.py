"""Admin dashboard API.

Lightweight admin endpoints that power the /admin UI.  All endpoints are
protected by the same ADMIN_TOKEN env var that guards PUT /v1/halt.

Routes
------
GET  /admin/api/status      — engine + DB + halt state snapshot
GET  /admin/api/trades      — recent trades from DB (read-only)
POST /admin/api/halt        — toggle halt switch (proxy to /v1/halt)
POST /admin/api/test-order  — server-side HMAC sign + POST to /v1/orders
GET  /admin/api/broker-accounts — list active broker accounts (for the dropdown)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Optional, Union

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api", tags=["admin"])


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def require_admin(
    x_tc_admin_token: str = Header(..., alias="X-TC-Admin-Token"),
) -> None:
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured")
    if not hmac.compare_digest(x_tc_admin_token, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


# ---------------------------------------------------------------------------
# GET /admin/api/status
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    version: str
    halted: bool
    halt_reason: Optional[str] = None
    db_connected: bool
    db_error: Optional[str] = None
    trade_count_24h: int = 0
    hmac_secret_set: bool
    hmac_next_secret_set: bool
    broker_master_key_set: bool
    admin_token_set: bool


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_admin)])
async def get_status(db: Session = Depends(get_db)) -> StatusResponse:
    from app.routers.health import _VERSION, _read_halt

    halt = _read_halt()

    db_ok = True
    db_err: Optional[str] = None
    trade_count = 0
    try:
        result = db.execute(
            text(
                "SELECT COUNT(*) FROM broker_orders "
                "WHERE order_time > NOW() - INTERVAL '24 hours'"
            )
        )
        trade_count = int(result.scalar() or 0)
    except Exception as exc:
        db_err = str(exc)[:200]
        # Connectivity might still be OK; try a SELECT 1
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            db_ok = False

    return StatusResponse(
        version=_VERSION,
        halted=halt.halted,
        halt_reason=halt.reason,
        db_connected=db_ok,
        db_error=db_err,
        trade_count_24h=trade_count,
        hmac_secret_set=bool(os.environ.get("EXECUTION_HMAC_SECRET")),
        hmac_next_secret_set=bool(os.environ.get("EXECUTION_HMAC_SECRET_NEXT")),
        broker_master_key_set=bool(os.environ.get("BROKER_ENCRYPTION_KEY")),
        admin_token_set=bool(os.environ.get("ADMIN_TOKEN")),
    )


# ---------------------------------------------------------------------------
# GET /admin/api/trades
# ---------------------------------------------------------------------------

@router.get("/trades", dependencies=[Depends(require_admin)])
async def list_trades(
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    try:
        rows = db.execute(
            text(
                """
                SELECT
                    bo.id, bo.symbol, bo.exchange, bo.transaction_type,
                    bo.quantity, bo.price, bo.order_type, bo.product_type,
                    bo.order_status, bo.status_message, bo.broker_order_id,
                    bo.correlation_id, bo.order_time, bo.tenant_id,
                    ub.broker_type, ub.user_id
                FROM broker_orders bo
                LEFT JOIN user_brokers ub ON ub.id = bo.broker_account_id
                ORDER BY bo.order_time DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()

        return {
            "trades": [
                {
                    "order_id": int(r["id"]),
                    "symbol": r["symbol"],
                    "exchange": r["exchange"],
                    "side": r["transaction_type"],
                    "quantity": r["quantity"],
                    "price": float(r["price"]) if r["price"] is not None else 0.0,
                    "order_type": r["order_type"],
                    "product_type": r["product_type"],
                    "status": r["order_status"],
                    "status_message": r["status_message"],
                    "broker_order_id": r["broker_order_id"] or "",
                    "broker_type": r["broker_type"] or "",
                    "user_id": int(r["user_id"]) if r["user_id"] is not None else None,
                    "tenant_id": r["tenant_id"],
                    "correlation_id": r["correlation_id"] or "",
                    "created_at": r["order_time"].isoformat() if r["order_time"] else "",
                }
                for r in rows
            ]
        }
    except Exception as exc:
        return {"trades": [], "error": str(exc)[:300]}


# ---------------------------------------------------------------------------
# GET /admin/api/broker-accounts
# ---------------------------------------------------------------------------

@router.get("/broker-accounts", dependencies=[Depends(require_admin)])
async def list_broker_accounts(db: Session = Depends(get_db)) -> dict[str, Any]:
    """List active user_brokers rows — what the engine can place orders against."""
    try:
        rows = db.execute(
            text(
                """
                SELECT ub.id, ub.user_id, ub.broker_type, ub.broker_name,
                       ub.is_active, ub.connection_status, ub.tenant_id,
                       u.username, u.email
                FROM user_brokers ub
                LEFT JOIN "user" u ON u.id = ub.user_id
                WHERE ub.is_active = true
                ORDER BY u.username NULLS LAST, ub.id
                LIMIT 100
                """
            )
        ).mappings().all()

        return {
            "accounts": [
                {
                    "user_broker_id": int(r["id"]),
                    "user_id": int(r["user_id"]),
                    "broker_type": r["broker_type"] or r["broker_name"],
                    "broker_name": r["broker_name"],
                    "connection_status": r["connection_status"],
                    "tenant_id": r["tenant_id"],
                    "username": r["username"] or "",
                    "email": r["email"] or "",
                }
                for r in rows
            ]
        }
    except Exception as exc:
        return {"accounts": [], "error": str(exc)[:300]}


# ---------------------------------------------------------------------------
# POST /admin/api/halt
# ---------------------------------------------------------------------------

class HaltToggle(BaseModel):
    halted: bool
    reason: Optional[str] = None


@router.post("/halt", dependencies=[Depends(require_admin)])
async def toggle_halt(body: HaltToggle) -> dict[str, Any]:
    from app.routers.health import _read_halt, _write_halt
    from shared.schemas import HaltState

    state = HaltState(halted=body.halted, reason=body.reason)
    _write_halt(state)
    logger.info("Admin UI set halt=%s reason=%r", state.halted, state.reason)
    return state.model_dump()


# ---------------------------------------------------------------------------
# POST /admin/api/test-order
# ---------------------------------------------------------------------------

class TestOrderRequest(BaseModel):
    user_id: int
    user_broker_id: int
    symbol: str
    trading_symbol: Optional[str] = None
    exchange: str = "NSE_EQ"           # Dhan segment code
    security_id: str
    transaction_type: str               # BUY / SELL
    quantity: int
    order_type: str = "MARKET"          # MARKET / LIMIT / SL / SL_M
    product_type: str = "CNC"           # INTRADAY / DELIVERY / CNC / MIS
    price: float = 0.0
    tag: str = "admin-ui"
    tenant_id: str = "live"


@router.post("/test-order", dependencies=[Depends(require_admin)])
async def place_test_order(body: TestOrderRequest) -> dict[str, Any]:
    """Server-side HMAC sign and POST to /v1/orders.

    Calls the engine over loopback so the full HMAC + idempotency + broker
    pipeline is exercised end-to-end.
    """
    secret = os.environ.get("EXECUTION_HMAC_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="EXECUTION_HMAC_SECRET not configured")

    payload = body.model_dump(mode="json")
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    message = f"{timestamp}.".encode() + raw_body
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

    request_id = f"admin-ui-{uuid.uuid4().hex[:8]}"
    idem_key = f"admin-ui-{uuid.uuid4().hex}"

    port = os.environ.get("PORT", "5000")
    url = f"http://127.0.0.1:{port}/v1/orders"

    headers = {
        "Content-Type": "application/json",
        "X-TC-Signature": signature,
        "X-TC-Timestamp": timestamp,
        "X-TC-Request-ID": request_id,
        "X-TC-Idempotency": idem_key,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, content=raw_body, headers=headers)
    except Exception as exc:
        logger.error("Admin test-order — engine call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"engine call failed: {exc}")

    try:
        body_json = resp.json()
    except Exception:
        body_json = {"raw_text": resp.text}

    return {
        "http_status": resp.status_code,
        "request_id": request_id,
        "idempotency_key": idem_key,
        "response": body_json,
    }
