"""Order endpoints — bound to TC's real schema.

POST   /v1/orders              — place an order (inserts into broker_orders)
POST   /v1/orders/{id}/cancel  — cancel an order
GET    /v1/orders/{id}         — get order status (refresh from broker)

`{id}` is the integer PK of broker_orders, NOT the broker's own order id.

All endpoints require valid HMAC signature.
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.middleware.hmac_auth import verify_hmac
from app.middleware.idempotency import IdempotencyCache, get_idempotency_cache
from app.routers.health import is_halted
from shared.brokers import get_executor
from shared.crypto import decrypt
from shared.db import get_db
from shared.models import BrokerOrder, User, UserBroker
from shared.schemas import (
    CancelOrderRequest,
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


def _fetch_user_or_404(db: Session, user_id: int) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=404, detail=f"user_not_found: no user with id={user_id}"
        )
    if user.active is False:
        raise HTTPException(
            status_code=404, detail=f"user_inactive: user {user_id} is disabled"
        )
    return user


def _fetch_user_broker_or_404(
    db: Session, user_broker_id: int, user_id: int
) -> UserBroker:
    ub = db.query(UserBroker).filter(UserBroker.id == user_broker_id).first()
    if ub is None:
        raise HTTPException(
            status_code=404,
            detail=f"user_broker_not_found: no user_brokers row with id={user_broker_id}",
        )
    if ub.user_id != user_id:
        raise HTTPException(
            status_code=404,
            detail=f"user_broker_owner_mismatch: user_brokers {user_broker_id} does not belong to user {user_id}",
        )
    if ub.is_active is False:
        raise HTTPException(
            status_code=404,
            detail=f"user_broker_inactive: user_brokers {user_broker_id} is disabled",
        )
    return ub


def _resolve_broker_creds(ub: UserBroker) -> tuple[str, str, str]:
    """Decrypt the credentials needed to talk to the broker.

    Returns (broker_type, client_id, access_token).

    For Dhan: client_id is stored in api_key, access_token in access_token.
    Both are Fernet-encrypted with BROKER_MASTER_KEY.
    """
    broker_type = (ub.broker_type or ub.broker_name or "").lower().strip()
    if not broker_type:
        raise HTTPException(status_code=500, detail="user_broker has no broker_type")
    try:
        client_id = decrypt(ub.api_key) if ub.api_key else ""
        access_token = decrypt(ub.access_token) if ub.access_token else ""
    except Exception as exc:
        logger.error("Failed to decrypt broker credentials: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to decrypt broker credentials")
    if not client_id or not access_token:
        raise HTTPException(
            status_code=500, detail="user_broker is missing api_key or access_token"
        )
    return broker_type, client_id, access_token


def _to_response(bo: BrokerOrder, broker_type: str) -> OrderResponse:
    return OrderResponse(
        order_id=bo.id,
        broker_order_id=bo.broker_order_id or "",
        status=bo.order_status or "PENDING",
        symbol=bo.symbol,
        exchange=bo.exchange,
        transaction_type=bo.transaction_type,
        quantity=bo.quantity,
        order_type=bo.order_type,
        product_type=bo.product_type,
        price=float(bo.price or 0),
        broker_type=broker_type,
    )


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

    # 2. Idempotency. Key is SCOPED by (user_id, user_broker_id, X-TC-Idempotency)
    #    so two different users using the same key string can't be conflated and
    #    one cannot receive the other's order response. The same scoping is
    #    applied to the DB lookup below.
    #
    #    NB: broker_orders.correlation_id has no DB UNIQUE constraint (we don't
    #    own TC's schema), so this read-then-insert pattern still races under
    #    truly concurrent retries on a single key. The in-process cache absorbs
    #    same-instance retries; cross-instance concurrent retries on the same
    #    key are out of scope for Phase 1 (single Railway dyno).
    scoped_key = (
        f"{body.user_id}:{body.user_broker_id}:{x_tc_idempotency}"
        if x_tc_idempotency
        else ""
    )
    if scoped_key and scoped_key in cache:
        logger.info("%s Idempotency cache hit key=%r", prefix, scoped_key)
        return cache.get(scoped_key)

    # 3. DB-level idempotency via broker_orders.correlation_id, scoped to the
    #    same (user, user_broker) so it can't return another user's order.
    if x_tc_idempotency:
        existing = (
            db.query(BrokerOrder)
            .filter(
                BrokerOrder.correlation_id == x_tc_idempotency,
                BrokerOrder.broker_account_id == body.user_broker_id,
            )
            .first()
        )
        if existing:
            # Defensive double-check: the user_broker row must belong to the
            # caller. (broker_account_id alone isn't enough — somebody could
            # have transferred the user_broker since.)
            existing_ub = existing.user_broker
            if existing_ub is None or existing_ub.user_id != body.user_id:
                logger.warning(
                    "%s DB idempotency hit key=%r but owner mismatch — refusing to return",
                    prefix, x_tc_idempotency,
                )
                raise HTTPException(
                    status_code=409,
                    detail="idempotency_key_collision: key already used by another caller",
                )
            logger.info(
                "%s DB idempotency hit key=%r order_id=%s",
                prefix, x_tc_idempotency, existing.id,
            )
            broker_type = existing_ub.broker_type or ""
            resp = _to_response(existing, broker_type)
            cache.set(scoped_key, resp.model_dump())
            return resp

    # 4. Validate user + user_broker (clean 404s, not 500s)
    _fetch_user_or_404(db, body.user_id)
    ub = _fetch_user_broker_or_404(db, body.user_broker_id, body.user_id)
    broker_type, client_id, access_token = _resolve_broker_creds(ub)

    # 5. INSERT broker_orders row (status=PENDING) so we have an id before
    #    calling the broker. If the broker call fails we UPDATE status=REJECTED.
    now = datetime.datetime.utcnow()
    trading_symbol = body.trading_symbol or body.symbol
    bo = BrokerOrder(
        broker_account_id=ub.id,                     # FK → user_brokers.id
        broker_order_id=None,                         # filled in after broker accepts
        correlation_id=x_tc_idempotency or None,
        symbol=body.symbol,
        trading_symbol=trading_symbol,
        exchange=body.exchange,
        security_id=body.security_id,
        transaction_type=body.transaction_type,
        order_type=body.order_type,
        product_type=body.product_type,
        quantity=body.quantity,
        filled_quantity=0,
        pending_quantity=body.quantity,
        price=body.price or 0.0,
        trigger_price=body.trigger_price or 0.0,
        disclosed_quantity=body.disclosed_quantity or 0,
        order_status="PENDING",
        status_message="",
        avg_execution_price=0.0,
        trading_signal_id=body.trading_signal_id,
        tenant_id=body.tenant_id or ub.tenant_id or "live",
        order_time=now,
        last_updated=now,
    )
    db.add(bo)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        logger.warning(
            "%s broker_orders insert IntegrityError: %s",
            prefix, getattr(exc, "orig", exc),
        )
        raise HTTPException(
            status_code=404,
            detail=f"foreign_key_violation: {getattr(exc, 'orig', exc)}",
        )

    # 6. Place the order with the broker.
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
        executor = get_executor(broker_type, client_id, access_token)
        result = executor.place_order(order_params)
    except NotImplementedError as exc:
        bo.order_status = "REJECTED"
        bo.status_message = str(exc)[:255]
        bo.last_updated = datetime.datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=501, detail=str(exc))
    except Exception as exc:
        logger.error("%s Broker place_order failed: %s", prefix, exc)
        bo.order_status = "REJECTED"
        bo.status_message = str(exc)[:255]
        bo.last_updated = datetime.datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=502, detail=f"broker_error: {exc}")

    # 7. Update the row with the broker's order id + accepted status
    bo.broker_order_id = str(result.get("broker_order_id", ""))
    bo.order_status = (result.get("status") or "PENDING").upper()
    bo.last_updated = datetime.datetime.utcnow()
    db.commit()
    db.refresh(bo)

    logger.info(
        "%s Order placed order_id=%s broker_order_id=%s broker=%s",
        prefix, bo.id, bo.broker_order_id, broker_type,
    )

    resp = _to_response(bo, broker_type)
    if scoped_key:
        cache.set(scoped_key, resp.model_dump())
    return resp


# ---------------------------------------------------------------------------
# POST /v1/orders/{order_id}/cancel
# ---------------------------------------------------------------------------

@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: int,
    body: CancelOrderRequest,
    request_id: Annotated[str, Depends(verify_hmac)],
    db: Session = Depends(get_db),
) -> Any:
    prefix = _log_prefix(request_id)

    bo = db.query(BrokerOrder).filter(BrokerOrder.id == order_id).first()
    if not bo:
        raise HTTPException(
            status_code=404, detail=f"order_not_found: no broker_orders row with id={order_id}"
        )

    # Authorization: the caller's user_broker_id must match, and that
    # user_broker must belong to body.user_id.
    if bo.broker_account_id != body.user_broker_id:
        raise HTTPException(
            status_code=404,
            detail=f"order_broker_mismatch: order {order_id} does not belong to user_broker {body.user_broker_id}",
        )
    ub = bo.user_broker
    if ub is None or ub.user_id != body.user_id:
        raise HTTPException(
            status_code=404,
            detail=f"order_owner_mismatch: order {order_id} does not belong to user {body.user_id}",
        )

    if not bo.broker_order_id:
        raise HTTPException(
            status_code=400,
            detail="order has no broker_order_id yet — nothing to cancel at the broker",
        )

    broker_type, client_id, access_token = _resolve_broker_creds(ub)
    try:
        executor = get_executor(broker_type, client_id, access_token)
        result = executor.cancel_order(bo.broker_order_id)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except Exception as exc:
        logger.error("%s Broker cancel_order failed: %s", prefix, exc)
        raise HTTPException(status_code=502, detail=f"broker_error: {exc}")

    bo.order_status = "CANCELLED"
    bo.status_message = json.dumps(result.get("raw", {}))[:255]
    bo.last_updated = datetime.datetime.utcnow()
    db.commit()
    db.refresh(bo)

    logger.info(
        "%s Order cancelled order_id=%s broker_order_id=%s",
        prefix, order_id, bo.broker_order_id,
    )
    return _to_response(bo, broker_type)


# ---------------------------------------------------------------------------
# GET /v1/orders/{order_id}
# ---------------------------------------------------------------------------

@router.get("/{order_id}", response_model=OrderStatusResponse)
async def get_order(
    order_id: int,
    request_id: Annotated[str, Depends(verify_hmac)],
    db: Session = Depends(get_db),
) -> Any:
    prefix = _log_prefix(request_id)

    bo = db.query(BrokerOrder).filter(BrokerOrder.id == order_id).first()
    if not bo:
        raise HTTPException(
            status_code=404, detail=f"order_not_found: no broker_orders row with id={order_id}"
        )

    if not bo.broker_order_id:
        # Order is in our DB but never reached the broker — return what we have.
        return OrderStatusResponse(
            order_id=bo.id,
            broker_order_id="",
            status=bo.order_status or "PENDING",
            filled_quantity=int(bo.filled_quantity or 0),
            average_price=float(bo.avg_execution_price) if bo.avg_execution_price else None,
            broker_raw={},
        )

    ub = bo.user_broker
    if ub is None:
        raise HTTPException(status_code=500, detail="order has no associated user_broker")

    broker_type, client_id, access_token = _resolve_broker_creds(ub)
    try:
        executor = get_executor(broker_type, client_id, access_token)
        result = executor.get_order_status(bo.broker_order_id)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except Exception as exc:
        logger.error("%s Broker get_order_status failed: %s", prefix, exc)
        raise HTTPException(status_code=502, detail=f"broker_error: {exc}")

    new_status = (result.get("status") or bo.order_status or "PENDING").upper()
    bo.order_status = new_status
    bo.status_message = json.dumps(result.get("raw", {}))[:255]
    bo.last_updated = datetime.datetime.utcnow()
    db.commit()

    return OrderStatusResponse(
        order_id=bo.id,
        broker_order_id=bo.broker_order_id,
        status=new_status,
        filled_quantity=int(bo.filled_quantity or 0),
        average_price=float(bo.avg_execution_price) if bo.avg_execution_price else None,
        broker_raw=result.get("raw", {}),
    )
