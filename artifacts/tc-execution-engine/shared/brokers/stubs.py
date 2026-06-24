"""Stub broker executors — brokers not yet fully implemented.

Currently stubbed:
  - Upstox (Phase 2)

Replace the class body with a real integration when adding support.
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


class UpstoxExecutor(BrokerExecutor):
    """Upstox — stub, not yet implemented (Phase 2)."""

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        raise _not_implemented("Upstox", "place_order")

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        raise _not_implemented("Upstox", "cancel_order")

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        raise _not_implemented("Upstox", "get_order_status")
