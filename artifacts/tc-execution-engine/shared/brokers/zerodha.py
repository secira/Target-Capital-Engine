"""Zerodha Kite Connect executor.

Credential mapping (stored encrypted in user_brokers):
  api_key column       → Kite API key  (from kite.trade developer console)
  access_token column  → Kite access_token (generated daily via the OAuth flow)

Exchange segments: TC stores Dhan segment codes (NSE_EQ, NSE_FNO, …).
This executor maps them to Kite's exchange names (NSE, NFO, …).

Symbol format: TC sends `trading_symbol` which must be in Kite's own format
(e.g. "RELIANCE" for NSE equity, "NIFTY24OCTFUT" for NFO futures).
The engine passes it through unchanged — Kite will reject unrecognised symbols.
"""
from __future__ import annotations

import logging
from typing import Any

from shared.brokers.base import BrokerExecutor

logger = logging.getLogger(__name__)


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return "<empty>"
    return (s[:keep] + "*" * max(0, len(s) - keep)) if len(s) > keep else "*" * len(s)


# TC (Dhan segment) → Kite exchange
_EXCHANGE_MAP: dict[str, str] = {
    "NSE_EQ": "NSE",
    "BSE_EQ": "BSE",
    "NSE_FNO": "NFO",
    "BSE_FNO": "BFO",
    "NSE_CURRENCY": "CDS",
    "BSE_CURRENCY": "BCD",
    "MCX_COMM": "MCX",
    # pass-through if already in Kite format
    "NSE": "NSE", "BSE": "BSE", "NFO": "NFO",
    "BFO": "BFO", "CDS": "CDS", "BCD": "BCD", "MCX": "MCX",
}

# TC order_type → Kite order_type
_ORDER_TYPE_MAP: dict[str, str] = {
    "MARKET": "MARKET",
    "LIMIT": "LIMIT",
    "SL": "SL",
    "SL_M": "SL-M",
    "SLM": "SL-M",
}

# TC product_type → Kite product
_PRODUCT_MAP: dict[str, str] = {
    "CNC": "CNC",
    "DELIVERY": "CNC",
    "MIS": "MIS",
    "INTRADAY": "MIS",
    "NRML": "NRML",
    "CARRYFORWARD": "NRML",
    "CO": "CO",
    "BO": "BO",
}

# Kite order status → TC broker_orders.orderstatus enum
_STATUS_MAP: dict[str, str] = {
    "COMPLETE": "COMPLETE",
    "REJECTED": "REJECTED",
    "CANCELLED": "CANCELLED",
    "OPEN": "OPEN",
    "TRIGGER PENDING": "PENDING",
    "AMO REQ RECEIVED": "PENDING",
    "PENDING": "PENDING",
    "OPEN PENDING": "PENDING",
    "MODIFY PENDING": "OPEN",
    "CANCEL PENDING": "OPEN",
}


class ZerodhaExecutor(BrokerExecutor):
    """Live Zerodha executor using the kiteconnect SDK."""

    def __init__(self, client_id: str, access_token: str) -> None:
        super().__init__(client_id, access_token)
        try:
            from kiteconnect import KiteConnect  # type: ignore[import]
            self._kite = KiteConnect(api_key=client_id, access_token=access_token)
        except ImportError as exc:
            raise RuntimeError(
                "kiteconnect is not installed. Run: pip install kiteconnect"
            ) from exc
        logger.info(
            "Zerodha SDK initialised api_key=%s token=%s",
            _mask(client_id), _mask(access_token, keep=6),
        )

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        exchange_raw = order_params.get("exchange_segment", "NSE_EQ")
        exchange = _EXCHANGE_MAP.get(exchange_raw, exchange_raw)
        order_type = _ORDER_TYPE_MAP.get(order_params.get("order_type", "MARKET"), "MARKET")
        product = _PRODUCT_MAP.get(order_params.get("product_type", "CNC"), "CNC")
        trading_symbol = order_params.get("trading_symbol") or order_params.get("symbol", "")
        price = float(order_params.get("price") or 0)
        trigger_price = float(order_params.get("trigger_price") or 0)
        disclosed_qty = int(order_params.get("disclosed_quantity") or 0)

        # Kite requires market_protection for all MARKET orders submitted via API.
        # It is the maximum % deviation from LTP that Zerodha will allow before
        # rejecting the fill.  Default: 1 % — can be overridden in order_params.
        # Reference: https://kite.trade/docs/connect/v3/orders/#regular-order-parameters
        market_protection: float | None = None
        if order_type == "MARKET":
            raw_mp = order_params.get("market_protection")
            market_protection = float(raw_mp) if raw_mp is not None else 1.0

        sdk_kwargs: dict[str, Any] = dict(
            variety="regular",
            exchange=exchange,
            tradingsymbol=trading_symbol,
            transaction_type=(order_params.get("transaction_type") or "BUY").upper(),
            quantity=int(order_params["quantity"]),
            product=product,
            order_type=order_type,
            validity=order_params.get("validity", "DAY"),
        )
        if price:
            sdk_kwargs["price"] = price
        if trigger_price:
            sdk_kwargs["trigger_price"] = trigger_price
        if market_protection is not None:
            sdk_kwargs["market_protection"] = market_protection
        if disclosed_qty:
            sdk_kwargs["disclosed_quantity"] = disclosed_qty
        if order_params.get("tag"):
            sdk_kwargs["tag"] = order_params["tag"]

        logger.info(
            "Zerodha place_order api_key=%s payload=%s",
            _mask(self.client_id), sdk_kwargs,
        )

        try:
            order_id = self._kite.place_order(**sdk_kwargs)
        except Exception:
            logger.exception(
                "Zerodha SDK place_order threw api_key=%s payload=%s",
                _mask(self.client_id), sdk_kwargs,
            )
            raise

        # Kite returns the order_id string directly on success
        broker_order_id = str(order_id)
        logger.info(
            "Zerodha place_order ACCEPTED api_key=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": "OPEN",
            "raw": {"order_id": broker_order_id},
        }

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        logger.info(
            "Zerodha cancel_order api_key=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        try:
            resp = self._kite.cancel_order(variety="regular", order_id=broker_order_id)
        except Exception:
            logger.exception(
                "Zerodha SDK cancel_order threw api_key=%s broker_order_id=%s",
                _mask(self.client_id), broker_order_id,
            )
            raise
        logger.info(
            "Zerodha cancel_order ACCEPTED api_key=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": "CANCELLED",
            "raw": {"order_id": str(resp)},
        }

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        logger.info(
            "Zerodha get_order_status api_key=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        try:
            history = self._kite.order_history(broker_order_id)
        except Exception:
            logger.exception(
                "Zerodha SDK order_history threw api_key=%s broker_order_id=%s",
                _mask(self.client_id), broker_order_id,
            )
            raise

        if not history:
            raise RuntimeError(
                f"Zerodha returned empty order history for order_id={broker_order_id}"
            )

        # history is a list of state transitions — last entry is current state
        latest = history[-1]
        raw_status = (latest.get("status") or "UNKNOWN").upper()
        status = _STATUS_MAP.get(raw_status, raw_status)
        filled_qty = latest.get("filled_quantity", 0)
        avg_price = latest.get("average_price", 0.0)

        logger.info(
            "Zerodha get_order_status response api_key=%s broker_order_id=%s "
            "status=%s filled_qty=%s avg_price=%s",
            _mask(self.client_id), broker_order_id, status, filled_qty, avg_price,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": status,
            "filled_qty": int(filled_qty or 0),
            "avg_price": float(avg_price or 0.0),
            "raw": latest,
        }
