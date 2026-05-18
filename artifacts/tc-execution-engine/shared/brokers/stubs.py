"""Stub broker executors for Zerodha, Angel, and Upstox.

Phase 1: raise NotImplementedError so the factory correctly signals that these
brokers are not yet supported.  Replace each class body when adding a real
integration.

Every stub method logs a WARNING so an attempt to use an unsupported broker
shows up clearly in production logs instead of being a silent 501.
"""
from __future__ import annotations

import logging
from typing import Any

from shared.brokers.base import BrokerExecutor

logger = logging.getLogger(__name__)


def _not_implemented(broker: str, method: str) -> "NotImplementedError":
    msg = f"{broker} executor is not yet implemented (method={method})"
    logger.warning("STUB INVOKED — %s. Caller must use a different broker.", msg)
    return NotImplementedError(msg)


class ZerodhaExecutor(BrokerExecutor):
    """Zerodha (Kite) — stub, not yet implemented."""

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        raise _not_implemented("Zerodha", "place_order")

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        raise _not_implemented("Zerodha", "cancel_order")

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        raise _not_implemented("Zerodha", "get_order_status")


class AngelExecutor(BrokerExecutor):
    """Angel One (SmartAPI) — stub, not yet implemented."""

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        raise _not_implemented("Angel", "place_order")

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        raise _not_implemented("Angel", "cancel_order")

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        raise _not_implemented("Angel", "get_order_status")


class UpstoxExecutor(BrokerExecutor):
    """Upstox — stub, not yet implemented."""

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        raise _not_implemented("Upstox", "place_order")

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        raise _not_implemented("Upstox", "cancel_order")

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        raise _not_implemented("Upstox", "get_order_status")
