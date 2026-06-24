"""Angel One SmartAPI executor.

Credential mapping (stored encrypted in user_brokers):
  api_key column       → Angel One API key (from smartapi.angelbroking.com developer console)
  access_token column  → JWT access_token obtained via generateSession / TC's auth flow

Exchange segments: TC stores Dhan segment codes (NSE_EQ, NSE_FNO, …).
This executor maps them to Angel's exchange names (NSE, BSE, NFO, …).

Symbol format: TC sends `trading_symbol` (e.g. "SBIN-EQ" for NSE equity)
and `security_id` which is used as Angel's `symboltoken` (their internal numeric
token). Both must match Angel's master data — verify via their symbol master CSV.
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


# TC (Dhan segment) → Angel exchange
_EXCHANGE_MAP: dict[str, str] = {
    "NSE_EQ": "NSE",
    "BSE_EQ": "BSE",
    "NSE_FNO": "NFO",
    "BSE_FNO": "NFO",
    "NSE_CURRENCY": "CDS",
    "MCX_COMM": "MCX",
    # pass-through
    "NSE": "NSE", "BSE": "BSE", "NFO": "NFO",
    "CDS": "CDS", "MCX": "MCX",
}

# TC order_type → Angel ordertype
_ORDER_TYPE_MAP: dict[str, str] = {
    "MARKET": "MARKET",
    "LIMIT": "LIMIT",
    "SL": "STOPLOSS",
    "SL_M": "STOPLOSS_MARKET",
    "SLM": "STOPLOSS_MARKET",
}

# TC product_type → Angel producttype
_PRODUCT_MAP: dict[str, str] = {
    "CNC": "DELIVERY",
    "DELIVERY": "DELIVERY",
    "MIS": "INTRADAY",
    "INTRADAY": "INTRADAY",
    "NRML": "CARRYFORWARD",
    "CARRYFORWARD": "CARRYFORWARD",
    "BO": "BO",
    "MARGIN": "MARGIN",
}

# Angel status → TC broker_orders.orderstatus enum
_STATUS_MAP: dict[str, str] = {
    "complete": "COMPLETE",
    "rejected": "REJECTED",
    "cancelled": "CANCELLED",
    "open": "OPEN",
    "pending": "PENDING",
    "open pending": "PENDING",
    "modified": "OPEN",
    "trigger pending": "PENDING",
    "amo req received": "PENDING",
    "after market order req received": "PENDING",
}


class AngelExecutor(BrokerExecutor):
    """Live Angel One executor using the smartapi-python SDK."""

    def __init__(self, client_id: str, access_token: str) -> None:
        super().__init__(client_id, access_token)
        try:
            from SmartApi import SmartConnect  # type: ignore[import]
            self._obj = SmartConnect(api_key=client_id)
            # Inject the pre-obtained JWT access token directly — we skip
            # generateSession because TC already handled authentication.
            self._obj.access_token = access_token
        except ImportError as exc:
            raise RuntimeError(
                "smartapi-python is not installed. Run: pip install smartapi-python"
            ) from exc
        logger.info(
            "Angel SmartAPI initialised api_key=%s token=%s",
            _mask(client_id), _mask(access_token, keep=6),
        )

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        exchange_raw = order_params.get("exchange_segment", "NSE_EQ")
        exchange = _EXCHANGE_MAP.get(exchange_raw, exchange_raw)
        order_type = _ORDER_TYPE_MAP.get(order_params.get("order_type", "MARKET"), "MARKET")
        product_type = _PRODUCT_MAP.get(order_params.get("product_type", "DELIVERY"), "DELIVERY")
        trading_symbol = order_params.get("trading_symbol") or order_params.get("symbol", "")
        symbol_token = str(order_params.get("security_id") or "")

        orderparams = {
            "variety": "NORMAL",
            "tradingsymbol": trading_symbol,
            "symboltoken": symbol_token,
            "transactiontype": (order_params.get("transaction_type") or "BUY").upper(),
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": product_type,
            "duration": order_params.get("validity", "DAY"),
            "price": str(float(order_params.get("price") or 0)),
            "squareoff": "0",
            "stoploss": str(float(order_params.get("trigger_price") or 0)),
            "quantity": str(int(order_params["quantity"])),
        }

        logger.info(
            "Angel place_order api_key=%s payload=%s",
            _mask(self.client_id), orderparams,
        )

        try:
            resp = self._obj.placeOrder(orderparams)
        except Exception:
            logger.exception(
                "Angel SDK placeOrder threw api_key=%s payload=%s",
                _mask(self.client_id), orderparams,
            )
            raise

        # resp is a dict: {"status": true/false, "message": "...", "errorcode": "...", "data": "order_id"}
        if not resp.get("status"):
            msg = resp.get("message") or str(resp)
            logger.warning(
                "Angel place_order REJECTED api_key=%s response=%s",
                _mask(self.client_id), resp,
            )
            raise RuntimeError(f"Angel order placement failed: {msg}")

        broker_order_id = str(resp.get("data") or "")
        logger.info(
            "Angel place_order ACCEPTED api_key=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": "OPEN",
            "raw": resp,
        }

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        logger.info(
            "Angel cancel_order api_key=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        try:
            resp = self._obj.cancelOrder(variety="NORMAL", orderid=broker_order_id)
        except Exception:
            logger.exception(
                "Angel SDK cancelOrder threw api_key=%s broker_order_id=%s",
                _mask(self.client_id), broker_order_id,
            )
            raise

        if not resp.get("status"):
            msg = resp.get("message") or str(resp)
            logger.warning(
                "Angel cancel_order REJECTED api_key=%s broker_order_id=%s response=%s",
                _mask(self.client_id), broker_order_id, resp,
            )
            raise RuntimeError(f"Angel cancel failed: {msg}")

        logger.info(
            "Angel cancel_order ACCEPTED api_key=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": "CANCELLED",
            "raw": resp,
        }

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        logger.info(
            "Angel get_order_status api_key=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        try:
            resp = self._obj.orderBook()
        except Exception:
            logger.exception(
                "Angel SDK orderBook threw api_key=%s",
                _mask(self.client_id),
            )
            raise

        if not resp.get("status"):
            msg = resp.get("message") or str(resp)
            raise RuntimeError(f"Angel orderBook failed: {msg}")

        orders = resp.get("data") or []
        # Find the specific order in the book
        order = next(
            (o for o in orders if str(o.get("orderid", "")) == broker_order_id),
            None,
        )
        if order is None:
            raise RuntimeError(
                f"Angel: order {broker_order_id} not found in today's order book"
            )

        raw_status = (order.get("status") or "unknown").lower()
        status = _STATUS_MAP.get(raw_status, raw_status.upper())
        filled_qty = order.get("filledshares") or order.get("filled_quantity") or 0
        avg_price = order.get("averageprice") or order.get("average_price") or 0.0

        logger.info(
            "Angel get_order_status response api_key=%s broker_order_id=%s "
            "status=%s filled_qty=%s avg_price=%s",
            _mask(self.client_id), broker_order_id, status, filled_qty, avg_price,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": status,
            "filled_qty": int(filled_qty or 0),
            "avg_price": float(avg_price or 0.0),
            "raw": order,
        }
