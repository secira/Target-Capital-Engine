"""Alice Blue executor.

Credential mapping (stored encrypted in user_brokers):
  api_key column       → Alice Blue user_id (login ID / client code)
  access_token column  → Alice Blue API key / access token

Exchange segments: TC stores Dhan segment codes (NSE_EQ, NSE_FNO, …).
This executor maps them to Alice Blue exchange names (NSE, BSE, NFO, …).

Instrument lookup: Alice Blue requires an Instrument object for each order.
  - We attempt to fetch by token first (security_id → integer token).
  - If that fails we fall back to symbol-name lookup.
  - The token must match Alice Blue's master data. Verify via their master CSV.

Symbol format: TC sends `trading_symbol` (e.g. "INFY" for NSE equity).
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


# TC (Dhan segment) → Alice Blue exchange string
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

# Alice Blue status strings → TC broker_orders.orderstatus enum
_STATUS_MAP: dict[str, str] = {
    "complete": "COMPLETE",
    "completed": "COMPLETE",
    "rejected": "REJECTED",
    "cancelled": "CANCELLED",
    "open": "OPEN",
    "pending": "PENDING",
    "trigger pending": "PENDING",
    "amo req received": "PENDING",
}


class AlicebluExecutor(BrokerExecutor):
    """Live Alice Blue executor using the alice-blue SDK."""

    def __init__(self, client_id: str, access_token: str) -> None:
        super().__init__(client_id, access_token)
        try:
            from alice_blue import AliceBlue  # type: ignore[import]
            # alice_blue SDK: user_id = login ID, api_key = the access token
            self._alice = AliceBlue(user_id=client_id, api_key=access_token)
        except ImportError as exc:
            raise RuntimeError(
                "alice-blue is not installed. Run: pip install alice-blue"
            ) from exc
        logger.info(
            "Alice Blue SDK initialised user_id=%s token=%s",
            _mask(client_id), _mask(access_token, keep=6),
        )

    def _get_instrument(self, exchange: str, security_id: Any, trading_symbol: str) -> Any:
        """Return an Alice Blue Instrument object.

        Tries integer-token lookup first (fastest), falls back to symbol name.
        Raises RuntimeError if neither resolves — order cannot be placed.
        """
        from alice_blue import AliceBlue  # type: ignore[import]

        token = None
        if security_id:
            try:
                token = int(security_id)
            except (ValueError, TypeError):
                pass

        if token is not None:
            try:
                instrument = self._alice.get_instrument_by_token(exchange, token)
                if instrument:
                    return instrument
            except Exception as exc:
                logger.debug(
                    "Alice Blue instrument token lookup failed exchange=%s token=%s: %s",
                    exchange, token, exc,
                )

        # Fall back to symbol name lookup
        if trading_symbol:
            try:
                instrument = self._alice.get_instrument_by_symbol(exchange, trading_symbol)
                if instrument:
                    return instrument
            except Exception as exc:
                logger.debug(
                    "Alice Blue instrument symbol lookup failed exchange=%s symbol=%s: %s",
                    exchange, trading_symbol, exc,
                )

        raise RuntimeError(
            f"Alice Blue: could not resolve instrument for "
            f"exchange={exchange} security_id={security_id} symbol={trading_symbol}. "
            f"Verify these values match Alice Blue's master data."
        )

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        from alice_blue import OrderType, ProductType, TransactionType  # type: ignore[import]

        exchange_raw = order_params.get("exchange_segment", "NSE_EQ")
        exchange = _EXCHANGE_MAP.get(exchange_raw, exchange_raw)
        trading_symbol = order_params.get("trading_symbol") or order_params.get("symbol", "")
        security_id = order_params.get("security_id")

        instrument = self._get_instrument(exchange, security_id, trading_symbol)

        # Map TC order_type → AliceBlue OrderType enum
        tc_order_type = (order_params.get("order_type") or "MARKET").upper()
        order_type_map = {
            "MARKET": OrderType.Market,
            "LIMIT": OrderType.Limit,
            "SL": OrderType.StopLossLimit,
            "SL_M": OrderType.StopLossMarket,
            "SLM": OrderType.StopLossMarket,
        }
        order_type = order_type_map.get(tc_order_type, OrderType.Market)

        # Map TC product_type → AliceBlue ProductType enum
        tc_product = (order_params.get("product_type") or "DELIVERY").upper()
        product_map = {
            "CNC": ProductType.Delivery,
            "DELIVERY": ProductType.Delivery,
            "MIS": ProductType.Intraday,
            "INTRADAY": ProductType.Intraday,
            "NRML": ProductType.Delivery,
            "CO": ProductType.CoverOrder,
            "BO": ProductType.BracketOrder,
        }
        product_type = product_map.get(tc_product, ProductType.Delivery)

        # Map TC transaction_type → AliceBlue TransactionType enum
        tc_txn = (order_params.get("transaction_type") or "BUY").upper()
        transaction_type = (
            TransactionType.Buy if tc_txn == "BUY" else TransactionType.Sell
        )

        price = float(order_params.get("price") or 0) or None
        trigger_price = float(order_params.get("trigger_price") or 0) or None
        quantity = int(order_params["quantity"])

        sdk_kwargs: dict[str, Any] = dict(
            transaction_type=transaction_type,
            instrument=instrument,
            quantity=quantity,
            order_type=order_type,
            product_type=product_type,
        )
        if price:
            sdk_kwargs["price"] = price
        if trigger_price:
            sdk_kwargs["trigger_price"] = trigger_price

        logger.info(
            "Alice Blue place_order user_id=%s exchange=%s symbol=%s "
            "txn=%s qty=%s order_type=%s product=%s",
            _mask(self.client_id), exchange, trading_symbol,
            tc_txn, quantity, tc_order_type, tc_product,
        )

        try:
            resp = self._alice.place_order(**sdk_kwargs)
        except Exception:
            logger.exception(
                "Alice Blue SDK place_order threw user_id=%s",
                _mask(self.client_id),
            )
            raise

        # resp is a dict; oms_order_id is the broker order ID
        if isinstance(resp, dict) and resp.get("stat") == "Not_ok":
            msg = resp.get("emsg") or str(resp)
            logger.warning(
                "Alice Blue place_order REJECTED user_id=%s response=%s",
                _mask(self.client_id), resp,
            )
            raise RuntimeError(f"Alice Blue order placement failed: {msg}")

        broker_order_id = str(
            resp.get("NOrdNo") or resp.get("oms_order_id") or resp.get("order_id") or ""
            if isinstance(resp, dict) else resp
        )
        logger.info(
            "Alice Blue place_order ACCEPTED user_id=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": "OPEN",
            "raw": resp if isinstance(resp, dict) else {"order_id": broker_order_id},
        }

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        logger.info(
            "Alice Blue cancel_order user_id=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        try:
            resp = self._alice.cancel_order(oms_order_id=broker_order_id)
        except Exception:
            logger.exception(
                "Alice Blue SDK cancel_order threw user_id=%s broker_order_id=%s",
                _mask(self.client_id), broker_order_id,
            )
            raise

        if isinstance(resp, dict) and resp.get("stat") == "Not_ok":
            msg = resp.get("emsg") or str(resp)
            logger.warning(
                "Alice Blue cancel_order REJECTED user_id=%s broker_order_id=%s response=%s",
                _mask(self.client_id), broker_order_id, resp,
            )
            raise RuntimeError(f"Alice Blue cancel failed: {msg}")

        logger.info(
            "Alice Blue cancel_order ACCEPTED user_id=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": "CANCELLED",
            "raw": resp if isinstance(resp, dict) else {"order_id": broker_order_id},
        }

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        logger.info(
            "Alice Blue get_order_status user_id=%s broker_order_id=%s",
            _mask(self.client_id), broker_order_id,
        )
        try:
            history = self._alice.get_order_history(oms_order_id=broker_order_id)
        except Exception:
            logger.exception(
                "Alice Blue SDK get_order_history threw user_id=%s broker_order_id=%s",
                _mask(self.client_id), broker_order_id,
            )
            raise

        # history can be a list of state transitions or a single dict
        if isinstance(history, list):
            if not history:
                raise RuntimeError(
                    f"Alice Blue returned empty history for order {broker_order_id}"
                )
            latest = history[-1]
        else:
            latest = history or {}

        raw_status = (
            latest.get("order_status") or latest.get("Status") or "unknown"
        ).lower()
        status = _STATUS_MAP.get(raw_status, raw_status.upper())
        filled_qty = (
            latest.get("filled_quantity") or latest.get("Fillshares") or 0
        )
        avg_price = (
            latest.get("average_price") or latest.get("Avgprc") or 0.0
        )

        logger.info(
            "Alice Blue get_order_status response user_id=%s broker_order_id=%s "
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
