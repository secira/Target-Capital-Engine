"""Pydantic request / response schemas."""
from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Order request
# ---------------------------------------------------------------------------

class PlaceOrderRequest(BaseModel):
    user_id: uuid.UUID
    broker_account_id: uuid.UUID
    signal_id: Optional[uuid.UUID] = None

    symbol: str = Field(..., min_length=1, max_length=50)
    exchange: str = Field(..., pattern=r"^(NSE|BSE|NFO|BFO|MCX|CDS)$")
    security_id: str = Field(..., description="Broker-specific security/instrument token")

    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(..., gt=0)
    order_type: Literal["MARKET", "LIMIT", "SL", "SLM"]
    product_type: Literal["CNC", "MIS", "NRML", "MTF", "CO", "BO"]

    price: float = Field(default=0.0, ge=0)
    trigger_price: float = Field(default=0.0, ge=0)
    disclosed_quantity: int = Field(default=0, ge=0)
    validity: Literal["DAY", "IOC", "GTD"] = "DAY"
    after_market_order: bool = False
    tag: str = Field(default="", max_length=20)


class CancelOrderRequest(BaseModel):
    """Body for POST /v1/orders/{trade_id}/cancel.

    user_id + broker_account_id are required so the engine can verify the
    cancel request actually originates from the user who owns the trade.
    """
    user_id: uuid.UUID
    broker_account_id: uuid.UUID


# ---------------------------------------------------------------------------
# Order response
# ---------------------------------------------------------------------------

class OrderResponse(BaseModel):
    trade_id: uuid.UUID
    broker_order_id: str
    status: str
    symbol: str
    exchange: str
    transaction_type: str
    quantity: int
    order_type: str
    product_type: str
    price: float
    broker_type: str


class OrderStatusResponse(BaseModel):
    trade_id: uuid.UUID
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
