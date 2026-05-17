"""Order endpoints.

POST   /v1/orders              — place an order
POST   /v1/orders/{id}/cancel  — cancel an order
GET    /v1/orders/{id}         — get order status

All endpoints require valid HMAC signature (via verify_hmac dependency).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.middleware.hmac_auth import verify_hmac
from app.middleware.idempotency import IdempotencyCache, get_idempotency_cache
from app.routers.health import is_halted
from shared.brokers import get_executor
from shared.crypto import decrypt
from shared.db import get_db
from shared.models import BrokerAccount, BrokerOrder, Trade, User
from shared.schemas import (
    ErrorResponse,
    OrderResponse,
    OrderStatusResponse,
    PlaceOrderRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/orders", tags=["orders"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_prefix(request_id: str) -> str:
    return f"[request_id={request_id}]"


def _fetch_broker_account(db: Session, broker_account_id: uuid.UUID, user_id: uuid.UUID) -> BrokerAccount:
    acct = (
        db.query(BrokerAccount)
        .filter(
            BrokerAccount.id == broker_account_id,
            BrokerAccount.user_id == user_id,
            BrokerAccount.is_active.is_(True),
        )
        .first()
    )
    if acct is None:
        raise HTTPException(status_code=404, detail="Broker account not found or inactive")
    return acct


# ---------------------------------------------------------------------------
# POST /v1/orders
# ---------------------------------------------------------------------------

@router.post("", response_model=OrderResponse, status_code=201)
async def place_order(
    body: PlaceOrderRequest,
    request_id: Annotated[str, Depends(verify_hmac)],
    x_tc_idempotency: str = Header(default="", alias="X-TC-Idempotency"),
    db: Session = Depends(get_db),
    cache: IdempotencyCache = Depends(get_idempotency_cache),
) -> Any:
    prefix = _log_prefix(request_id)

    # 1. Halt check
    if is_halted():
        logger.info("%s Order rejected — engine halted", prefix)
        raise HTTPException(status_code=503, detail="halted")

    # 2. Idempotency check
    if x_tc_idempotency and x_tc_idempotency in cache:
        logger.info("%s Idempotency hit key=%r", prefix, x_tc_idempotency)
        return cache.get(x_tc_idempotency)

    # 3. Check for duplicate idempotency key in DB
    if x_tc_idempotency:
        existing_trade = (
            db.query(Trade)
            .filter(Trade.idempotency_key == x_tc_idempotency)
            .first()
        )
        if existing_trade:
            logger.info("%s DB idempotency hit key=%r trade_id=%s", prefix, x_tc_idempotency, existing_trade.id)
            broker_order = existing_trade.broker_orders[0] if existing_trade.broker_orders else None
            resp = OrderResponse(
                trade_id=existing_trade.id,
                broker_order_id=broker_order.broker_order_id if broker_order else "",
                status=existing_trade.status,
                symbol=existing_trade.symbol,
                exchange=existing_trade.exchange,
                transaction_type=existing_trade.transaction_type,
                quantity=existing_trade.quantity,
                order_type=existing_trade.order_type,
                product_type=existing_trade.product_type,
                price=float(existing_trade.price or 0),
                broker_type=broker_order.broker_type if broker_order else "",
            )
            cache.set(x_tc_idempotency, resp.model_dump())
            return resp

    # 4. Fetch broker account & decrypt credentials
    broker_acct = _fetch_broker_account(db, body.broker_account_id, body.user_id)
    try:
        access_token = decrypt(broker_acct.encrypted_access_token)
    except Exception as exc:
        logger.error("%s Failed to decrypt broker credentials: %s", prefix, exc)
        raise HTTPException(status_code=500, detail="Failed to decrypt broker credentials")

    # 5. Create Trade record (status=pending)
    trade = Trade(
        user_id=body.user_id,
        broker_account_id=broker_acct.id,
        signal_id=body.signal_id,
        symbol=body.symbol,
        exchange=body.exchange,
        transaction_type=body.transaction_type,
        quantity=body.quantity,
        price=body.price,
        order_type=body.order_type,
        product_type=body.product_type,
        status="pending",
        idempotency_key=x_tc_idempotency or None,
        request_id=request_id or None,
    )
    db.add(trade)
    db.flush()  # get trade.id without committing

    # 6. Place order with broker
    order_params = {
        "security_id": body.security_id,
        "exchange_segment": body.exchange,
        "transaction_type": body.transaction_type,
        "quantity": body.quantity,
        "order_type": body.order_type,
        "product_type": body.product_type,
        "price": body.price,
        "trigger_price": body.trigger_price,
        "disclosed_quantity": body.disclosed_quantity,
        "validity": body.validity,
        "after_market_order": body.after_market_order,
        "tag": body.tag,
    }

    try:
        executor = get_executor(broker_acct.broker_type, broker_acct.client_id, access_token)
        result = executor.place_order(order_params)
    except NotImplementedError as exc:
        trade.status = "rejected"
        trade.error_code = "broker_error"
        trade.error_message = str(exc)
        db.commit()
        raise HTTPException(status_code=501, detail=str(exc))
    except Exception as exc:
        logger.error("%s Broker place_order failed: %s", prefix, exc)
        trade.status = "rejected"
        trade.error_code = "broker_error"
        trade.error_message = str(exc)
        db.commit()
        raise HTTPException(status_code=502, detail=f"broker_error: {exc}")

    # 7. Write BrokerOrder and update Trade
    broker_order = BrokerOrder(
        trade_id=trade.id,
        broker_type=broker_acct.broker_type,
        broker_order_id=result["broker_order_id"],
        status=result.get("status", "pending"),
        raw_request=json.dumps(order_params),
        raw_response=json.dumps(result.get("raw", {})),
    )
    db.add(broker_order)
    trade.status = "placed"
    db.commit()
    db.refresh(broker_order)

    logger.info(
        "%s Order placed trade_id=%s broker_order_id=%s broker=%s",
        prefix, trade.id, result["broker_order_id"], broker_acct.broker_type,
    )

    resp = OrderResponse(
        trade_id=trade.id,
        broker_order_id=result["broker_order_id"],
        status=trade.status,
        symbol=trade.symbol,
        exchange=trade.exchange,
        transaction_type=trade.transaction_type,
        quantity=trade.quantity,
        order_type=trade.order_type,
        product_type=trade.product_type,
        price=float(trade.price or 0),
        broker_type=broker_acct.broker_type,
    )

    if x_tc_idempotency:
        cache.set(x_tc_idempotency, resp.model_dump())

    return resp


# ---------------------------------------------------------------------------
# POST /v1/orders/{id}/cancel
# ---------------------------------------------------------------------------

@router.post("/{trade_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    trade_id: uuid.UUID,
    request_id: Annotated[str, Depends(verify_hmac)],
    db: Session = Depends(get_db),
) -> Any:
    prefix = _log_prefix(request_id)

    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    broker_order = trade.broker_orders[0] if trade.broker_orders else None
    if not broker_order:
        raise HTTPException(status_code=400, detail="No broker order associated with this trade")

    broker_acct = trade.broker_account
    try:
        access_token = decrypt(broker_acct.encrypted_access_token)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to decrypt broker credentials")

    try:
        executor = get_executor(broker_acct.broker_type, broker_acct.client_id, access_token)
        result = executor.cancel_order(broker_order.broker_order_id)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except Exception as exc:
        logger.error("%s Broker cancel_order failed: %s", prefix, exc)
        raise HTTPException(status_code=502, detail=f"broker_error: {exc}")

    broker_order.status = "CANCELLED"
    broker_order.raw_response = json.dumps(result.get("raw", {}))
    trade.status = "cancelled"
    db.commit()

    logger.info("%s Order cancelled trade_id=%s broker_order_id=%s", prefix, trade_id, broker_order.broker_order_id)

    return OrderResponse(
        trade_id=trade.id,
        broker_order_id=broker_order.broker_order_id,
        status="cancelled",
        symbol=trade.symbol,
        exchange=trade.exchange,
        transaction_type=trade.transaction_type,
        quantity=trade.quantity,
        order_type=trade.order_type,
        product_type=trade.product_type,
        price=float(trade.price or 0),
        broker_type=broker_acct.broker_type,
    )


# ---------------------------------------------------------------------------
# GET /v1/orders/{id}
# ---------------------------------------------------------------------------

@router.get("/{trade_id}", response_model=OrderStatusResponse)
async def get_order(
    trade_id: uuid.UUID,
    request_id: Annotated[str, Depends(verify_hmac)],
    db: Session = Depends(get_db),
) -> Any:
    prefix = _log_prefix(request_id)

    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    broker_order = trade.broker_orders[0] if trade.broker_orders else None
    if not broker_order:
        raise HTTPException(status_code=400, detail="No broker order found")

    broker_acct = trade.broker_account
    try:
        access_token = decrypt(broker_acct.encrypted_access_token)
        executor = get_executor(broker_acct.broker_type, broker_acct.client_id, access_token)
        result = executor.get_order_status(broker_order.broker_order_id)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except Exception as exc:
        logger.error("%s Broker get_order_status failed: %s", prefix, exc)
        raise HTTPException(status_code=502, detail=f"broker_error: {exc}")

    # Update local record
    broker_order.status = result.get("status", broker_order.status)
    broker_order.raw_response = json.dumps(result.get("raw", {}))
    db.commit()

    return OrderStatusResponse(
        trade_id=trade.id,
        broker_order_id=broker_order.broker_order_id,
        status=broker_order.status,
        filled_quantity=broker_order.filled_quantity or 0,
        average_price=float(broker_order.average_price) if broker_order.average_price else None,
        broker_raw=result.get("raw", {}),
    )
