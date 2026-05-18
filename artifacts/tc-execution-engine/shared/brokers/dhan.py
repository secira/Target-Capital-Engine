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


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return "<empty>"
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


class DhanExecutor(BrokerExecutor):
    """Live Dhan executor using the dhanhq SDK."""

    def __init__(self, client_id: str, access_token: str) -> None:
        super().__init__(client_id, access_token)
        try:
            from dhanhq import dhanhq  # type: ignore[import]
            self._dhan = dhanhq(client_id, access_token)
        except ImportError as exc:
            logger.exception("dhanhq SDK import failed")
            raise RuntimeError(
                "dhanhq is not installed. Run: pip install dhanhq"
            ) from exc
        logger.info(
            "Dhan SDK initialised client_id=%s token=%s",
            _mask(client_id), _mask(access_token, keep=6),
        )

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        """Place an order via Dhan API.

        Expected keys in order_params:
          security_id, exchange_segment, transaction_type, quantity,
          order_type, product_type, price (for LIMIT), trigger_price (for SL),
          disclosed_quantity (optional), validity (optional, default DAY),
          tag (optional), after_market_order (optional, default False)
        """
        # TC stores Dhan's native segment codes already (NSE_EQ etc), so
        # pass `exchange_segment` straight through to the SDK.
        exchange = order_params.get("exchange_segment", "NSE_EQ")
        order_type = _ORDER_TYPE_MAP.get(order_params.get("order_type", "MARKET"), "MARKET")
        product_type = _PRODUCT_MAP.get(order_params.get("product_type", "CNC"), "CNC")

        # Log the request fully so we can debug a rejected order without
        # re-running it (no secrets in here).
        sdk_kwargs = dict(
            security_id=str(order_params["security_id"]),
            exchange_segment=exchange,
            transaction_type=(order_params.get("transaction_type") or "").upper(),
            quantity=int(order_params["quantity"]),
            order_type=order_type,
            product_type=product_type,
            price=float(order_params.get("price") or 0),
            trigger_price=float(order_params.get("trigger_price") or 0),
            disclosed_quantity=int(order_params.get("disclosed_quantity") or 0),
            after_market_order=bool(order_params.get("after_market_order", False)),
            validity=order_params.get("validity", "DAY"),
            amo_time=order_params.get("amo_time", "OPEN"),
            bo_profit_value=float(order_params.get("bo_profit_value") or 0),
            bo_stop_loss_Value=float(order_params.get("bo_stop_loss_value") or 0),
            tag=order_params.get("tag", "") or "",
        )
        logger.info(
            "Dhan place_order client_id=%s payload=%s",
            _mask(self.client_id), sdk_kwargs,
        )

        try:
            resp = self._dhan.place_order(**sdk_kwargs)
        except Exception:
            # Anything raised by the SDK (network, JSON parse, …) — log full
            # traceback and re-raise so orders.py records REJECTED + returns 502.
            logger.exception(
                "Dhan SDK place_order threw client_id=%s payload=%s",
                _mask(self.client_id), sdk_kwargs,
            )
            raise

        if resp.get("status") == "failure":
            # Dhan returned a structured error — log at WARNING so it stands
            # out in the logs, then raise so orders.py persists REJECTED.
            logger.warning(
                "Dhan place_order REJECTED client_id=%s response=%s",
                _mask(self.client_id), resp,
            )
            raise RuntimeError(f"Dhan order placement failed: {resp}")

        logger.info(
            "Dhan place_order ACCEPTED client_id=%s response=%s",
            _mask(self.client_id), resp,
        )
        return {
            "broker_order_id": str(resp.get("data", {}).get("orderId", "")),
            "status": resp.get("data", {}).get("orderStatus", "PENDING"),
            "raw": resp,
        }

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        logger.info(
            "Dhan cancel_order client_id=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        try:
            resp = self._dhan.cancel_order(order_id=broker_order_id)
        except Exception:
            logger.exception(
                "Dhan SDK cancel_order threw client_id=%s broker_order_id=%s",
                _mask(self.client_id), broker_order_id,
            )
            raise
        if resp.get("status") == "failure":
            logger.warning(
                "Dhan cancel_order REJECTED client_id=%s broker_order_id=%s response=%s",
                _mask(self.client_id), broker_order_id, resp,
            )
            raise RuntimeError(f"Dhan cancel failed: {resp}")
        logger.info(
            "Dhan cancel_order ACCEPTED client_id=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": "CANCELLED",
            "raw": resp,
        }

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        logger.info(
            "Dhan get_order_status client_id=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        try:
            resp = self._dhan.get_order_by_id(order_id=broker_order_id)
        except Exception:
            logger.exception(
                "Dhan SDK get_order_by_id threw client_id=%s broker_order_id=%s",
                _mask(self.client_id), broker_order_id,
            )
            raise
        # Treat a structured failure response as an error (same as place/cancel)
        # so the caller gets a 502 instead of a silent 200 with status=UNKNOWN.
        if resp.get("status") == "failure":
            logger.warning(
                "Dhan get_order_status REJECTED client_id=%s broker_order_id=%s response=%s",
                _mask(self.client_id), broker_order_id, resp,
            )
            raise RuntimeError(f"Dhan get_order_status failed: {resp}")
        data = resp.get("data") or {}
        status = data.get("orderStatus", "UNKNOWN")
        logger.info(
            "Dhan get_order_status response client_id=%s broker_order_id=%s status=%s",
            _mask(self.client_id), broker_order_id, status,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": status,
            "raw": resp,
        }
