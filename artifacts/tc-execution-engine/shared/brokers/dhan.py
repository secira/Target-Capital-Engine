"""Dhan broker executor.

Uses the dhan-tradehull SDK.  The SDK is imported lazily so the rest of the app
starts cleanly even if the package is not yet installed.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from shared.brokers.base import (
    BrokerExecutor,
    BrokerRejectedError,
    BrokerUnknownStateError,
)

logger = logging.getLogger(__name__)


def _fingerprint(s: str) -> str:
    """Non-reversible fingerprint of a secret so two systems can compare the
    EXACT bytes they each hold without ever logging the secret itself.

    Logs length, whether there's stray leading/trailing whitespace (a common
    cause of 'valid token but rejected'), the first 3 chars (JWTs start 'eyJ'),
    and a short sha256 prefix. Share the sha12 with TC's side: if they match,
    both systems are sending identical bytes and the token is simply expired;
    if they differ, the bytes diverge somewhere despite the same DB row.
    """
    if not s:
        return "<empty>"
    stripped = s.strip()
    sha12 = hashlib.sha256(s.encode()).hexdigest()[:12]
    return (
        f"len={len(s)} stripped_len={len(stripped)} "
        f"has_surrounding_ws={s != stripped} "
        f"prefix3={s[:3]!r} sha12={sha12}"
    )


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


# Dhan order-status vocabulary → TC's `orderstatus` Postgres enum.
#
# TC's enum only allows: PENDING, OPEN, COMPLETE, CANCELLED, REJECTED. Dhan
# uses a richer set (TRANSIT, TRADED, PART_TRADED, EXPIRED, …), so writing
# Dhan's raw status straight into the column raises InvalidTextRepresentation
# (e.g. "TRANSIT"). Map every Dhan status onto a valid enum member here; the
# full raw Dhan response is still preserved in broker_orders.status_message.
_STATUS_MAP = {
    "TRANSIT": "PENDING",          # accepted by Dhan, in transit to exchange
    "PENDING": "PENDING",
    "CONFIRM": "OPEN",             # confirmed / working at the exchange
    "OPEN": "OPEN",
    "TRADED": "COMPLETE",          # fully executed
    "EXECUTED": "COMPLETE",
    "COMPLETE": "COMPLETE",
    "COMPLETED": "COMPLETE",
    "PART_TRADED": "OPEN",         # partially filled, still working
    "PARTIALLY_FILLED": "OPEN",
    "PARTIAL": "OPEN",
    "REJECTED": "REJECTED",
    "CANCELLED": "CANCELLED",
    "CANCELED": "CANCELLED",
    "EXPIRED": "CANCELLED",        # lapsed / not executed
}


def _map_status(raw: str) -> str:
    """Map a Dhan order status onto TC's orderstatus enum.

    Unknown / unexpected statuses default to PENDING (a safe, valid member)
    and are logged so a newly-introduced Dhan status can never again break the
    DB write. The untouched raw value still lands in status_message.
    """
    key = (raw or "").strip().upper()
    mapped = _STATUS_MAP.get(key)
    if mapped is None:
        logger.warning(
            "Unmapped Dhan order status %r — defaulting to PENDING. "
            "Add it to _STATUS_MAP if this recurs.",
            raw,
        )
        return "PENDING"
    return mapped


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
            import dhanhq as _dhanhq_pkg  # type: ignore[import]
            from dhanhq import dhanhq  # type: ignore[import]
        except ImportError as exc:
            logger.exception("dhanhq SDK import failed")
            raise RuntimeError(
                "dhanhq is not installed. Run: pip install dhanhq"
            ) from exc

        # Which SDK (and therefore which Dhan API version) are we actually
        # running? A version mismatch vs TC is a prime DH-901 suspect: the
        # constructor signature and the API endpoint/auth header changed
        # between dhanhq v1 and v2.
        sdk_version = getattr(_dhanhq_pkg, "__version__", "unknown")
        logger.info(
            "Dhan SDK init: dhanhq_version=%s constructor=dhanhq(client_id, access_token)",
            sdk_version,
        )
        # Fingerprint exactly what we're about to hand the SDK — lets us prove
        # byte-for-byte whether the engine and TC send the same client_id/token.
        logger.info(
            "Dhan creds fingerprint: client_id_repr=%r client_id_fp=[%s] token_fp=[%s]",
            client_id, _fingerprint(client_id), _fingerprint(access_token),
        )

        self._dhan = dhanhq(client_id, access_token)
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
        except Exception as exc:
            # Transport-level failure (network, timeout, JSON parse, …). The
            # order MAY have reached Dhan — outcome is UNKNOWN. Signal that
            # explicitly so orders.py leaves it PENDING for reconciliation
            # instead of falsely marking it REJECTED.
            logger.exception(
                "Dhan SDK place_order threw (UNKNOWN STATE) client_id=%s payload=%s",
                _mask(self.client_id), sdk_kwargs,
            )
            raise BrokerUnknownStateError(
                f"Dhan place_order did not confirm (possible phantom order): {exc}"
            ) from exc

        if resp.get("status") == "failure":
            # Dhan returned a structured error — the order was DEFINITIVELY
            # rejected and no live order exists. Safe to persist REJECTED.
            logger.warning(
                "Dhan place_order REJECTED client_id=%s response=%s",
                _mask(self.client_id), resp,
            )
            raise BrokerRejectedError(f"Dhan order placement failed: {resp}")

        logger.info(
            "Dhan place_order ACCEPTED client_id=%s response=%s",
            _mask(self.client_id), resp,
        )
        raw_status = resp.get("data", {}).get("orderStatus", "PENDING")
        return {
            "broker_order_id": str(resp.get("data", {}).get("orderId", "")),
            "status": _map_status(raw_status),     # TC enum-safe (e.g. TRANSIT→PENDING)
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
        filled_qty = data.get("filledQty") or data.get("filled_qty") or 0
        avg_price = data.get("averageTradedPrice") or data.get("avg_price") or 0.0
        mapped_status = _map_status(status)
        logger.info(
            "Dhan get_order_status response client_id=%s broker_order_id=%s "
            "status=%s (raw=%s) filled_qty=%s avg_price=%s",
            _mask(self.client_id), broker_order_id, mapped_status, status,
            filled_qty, avg_price,
        )
        return {
            "broker_order_id": broker_order_id,
            "status": mapped_status,               # TC enum-safe
            "filled_qty": int(filled_qty),
            "avg_price": float(avg_price),
            "raw": resp,
        }
