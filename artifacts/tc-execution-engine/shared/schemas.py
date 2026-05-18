"""Pydantic request/response schemas — aligned with TC's real DB.

All IDs are integers (TC uses integer PKs everywhere). Enum values match the
Postgres enums on `broker_orders` exactly.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Order request
# ---------------------------------------------------------------------------

# Enum values — keep in lock-step with Postgres enums on `broker_orders`.
TransactionType = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "SL", "SL_M"]
ProductType = Literal["INTRADAY", "DELIVERY", "CNC", "MIS"]
OrderStatus = Literal["PENDING", "OPEN", "COMPLETE", "CANCELLED", "REJECTED"]


class PlaceOrderRequest(BaseModel):
    """POST /v1/orders body.

    `user_broker_id` is the integer PK from TC's `user_brokers` table — the
    broker connection row whose encrypted access_token will be used to place
    the order. `user_id` is verified to own that connection.

    `exchange` is a free-form string passed straight to the broker (e.g.
    "NSE_EQ", "BSE_EQ", "NSE_FNO" for Dhan). `trading_symbol` defaults to
    `symbol` if omitted.
    """

    user_id: int
    user_broker_id: int
    trading_signal_id: Optional[int] = None

    symbol: str = Field(..., min_length=1, max_length=50)
    trading_symbol: Optional[str] = Field(default=None, max_length=50)
    exchange: str = Field(..., min_length=1, max_length=20)
    security_id: str = Field(..., description="Broker-specific instrument token")

    transaction_type: TransactionType
    quantity: int = Field(..., gt=0)
    order_type: OrderType
    product_type: ProductType

    price: float = Field(default=0.0, ge=0)
    trigger_price: float = Field(default=0.0, ge=0)
    disclosed_quantity: int = Field(default=0, ge=0)
    validity: Literal["DAY", "IOC", "GTD"] = "DAY"   # broker hint, not stored
    after_market_order: bool = False
    tag: str = Field(default="", max_length=40)
    tenant_id: str = Field(default="live", max_length=40)


class CancelOrderRequest(BaseModel):
    """POST /v1/orders/{order_id}/cancel body.

    user_id + user_broker_id are required so the engine can verify the cancel
    request actually originates from the owner of the order.
    """

    user_id: int
    user_broker_id: int


# ---------------------------------------------------------------------------
# Order response
# ---------------------------------------------------------------------------

class OrderResponse(BaseModel):
    """Returned by POST /v1/orders and POST /v1/orders/{id}/cancel."""

    order_id: int                                  # broker_orders.id
    broker_order_id: str                            # broker_orders.broker_order_id
    status: str                                     # mirror of order_status enum
    symbol: str
    exchange: str
    transaction_type: str
    quantity: int
    order_type: str
    product_type: str
    price: float
    broker_type: str                                # "dhan", "zerodha", ...


class OrderStatusResponse(BaseModel):
    """Returned by GET /v1/orders/{id}."""

    order_id: int
    broker_order_id: str
    status: str
    filled_quantity: int
    average_price: Optional[float]
    broker_raw: dict[str, Any]


# ---------------------------------------------------------------------------
# Halt
# ---------------------------------------------------------------------------

class HaltState(BaseModel):
    halted: bool
    reason: Optional[str] = None


class SetHaltRequest(BaseModel):
    halted: bool
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    error: Literal["broker_error", "validation_error", "auth_error", "halted", "not_found"]
    message: str
    request_id: Optional[str] = None
