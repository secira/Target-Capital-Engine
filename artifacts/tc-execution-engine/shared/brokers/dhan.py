"""Dhan broker executor.

Uses the dhan-tradehull SDK.  The SDK is imported lazily so the rest of the app
starts cleanly even if the package is not yet installed.
"""
from __future__ import annotations

import logging
from typing import Any

from shared.brokers.base import BrokerExecutor

logger = logging.getLogger(__name__)


# Dhan order type / exchange mappings.
#
# TC's `broker_orders` enums use values like SL_M / INTRADAY / DELIVERY / CNC
# / MIS, but the dhanhq SDK expects STOP_LOSS / STOP_LOSS_MARKET / CNC /
# INTRADAY / MARGIN. Map from TC's enum value → Dhan SDK value here.
#
# Exchange is passed through unchanged — TC stores Dhan's native segment
# codes (NSE_EQ, BSE_EQ, NSE_FNO, etc).
_ORDER_TYPE_MAP = {
    "MARKET": "MARKET",
    "LIMIT": "LIMIT",
    "SL": "STOP_LOSS",
    "SL_M": "STOP_LOSS_MARKET",
    # tolerate legacy / alternate spellings
    "SLM": "STOP_LOSS_MARKET",
}

_PRODUCT_MAP = {
    "CNC": "CNC",
    "DELIVERY": "CNC",
    "MIS": "INTRADAY",
    "INTRADAY": "INTRADAY",
    # legacy aliases
    "NRML": "MARGIN",
    "MTF": "MTF",
    "CO": "CO",
    "BO": "BO",
}


class DhanExecutor(BrokerExecutor):
    """Live Dhan executor using dhan-tradehull."""

    def __init__(self, client_id: str, access_token: str) -> None:
        super().__init__(client_id, access_token)
        try:
            from dhanhq import dhanhq  # type: ignore[import]
            self._dhan = dhanhq(client_id, access_token)
        except ImportError as exc:
            raise RuntimeError(
                "dhan-tradehull is not installed. Run: pip install dhan-tradehull"
            ) from exc

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        """Place an order via Dhan API.

        Expected keys in order_params:
          security_id, exchange_segment, transaction_type, quantity,
          order_type, product_type, price (for LIMIT), trigger_price (for SL),
          disclosed_quantity (optional), validity (optional, default DAY),
          tag (optional), after_market_order (optional, default False)
        """
        from dhanhq import dhanhq as _dh  # noqa: F401 – ensure import

        # TC stores Dhan's native segment codes already (NSE_EQ etc), so
        # pass `exchange_segment` straight through to the SDK.
        exchange = order_params.get("exchange_segment", "NSE_EQ")
        order_type = _ORDER_TYPE_MAP.get(order_params.get("order_type", "MARKET"), "MARKET")
        product_type = _PRODUCT_MAP.get(order_params.get("product_type", "CNC"), "CNC")

        resp = self._dhan.place_order(
            security_id=str(order_params["security_id"]),
            exchange_segment=exchange,
            transaction_type=order_params["transaction_type"].upper(),  # BUY / SELL
            quantity=int(order_params["quantity"]),
            order_type=order_type,
            product_type=product_type,
            price=float(order_params.get("price", 0)),
            trigger_price=float(order_params.get("trigger_price", 0)),
            disclosed_quantity=int(order_params.get("disclosed_quantity", 0)),
            after_market_order=bool(order_params.get("after_market_order", False)),
            validity=order_params.get("validity", "DAY"),
            amo_time=order_params.get("amo_time", "OPEN"),
            bo_profit_value=float(order_params.get("bo_profit_value", 0)),
            bo_stop_loss_Value=float(order_params.get("bo_stop_loss_value", 0)),
            tag=order_params.get("tag", ""),
        )

        logger.debug("Dhan place_order raw response: %s", resp)

        if resp.get("status") == "failure":
            raise RuntimeError(f"Dhan order placement failed: {resp}")

        return {
            "broker_order_id": str(resp.get("data", {}).get("orderId", "")),
            "status": resp.get("data", {}).get("orderStatus", "PENDING"),
            "raw": resp,
        }

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        resp = self._dhan.cancel_order(order_id=broker_order_id)
        logger.debug("Dhan cancel_order raw response: %s", resp)
        if resp.get("status") == "failure":
            raise RuntimeError(f"Dhan cancel failed: {resp}")
        return {
            "broker_order_id": broker_order_id,
            "status": "CANCELLED",
            "raw": resp,
        }

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        resp = self._dhan.get_order_by_id(order_id=broker_order_id)
        logger.debug("Dhan get_order_status raw response: %s", resp)
        data = resp.get("data", {})
        return {
            "broker_order_id": broker_order_id,
            "status": data.get("orderStatus", "UNKNOWN"),
            "raw": resp,
        }
