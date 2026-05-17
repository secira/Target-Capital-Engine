"""Stub broker executors for Zerodha, Angel, and Upstox.

Phase 1: raise NotImplementedError so the factory correctly signals that these
brokers are not yet supported.  Replace each class body when adding a real
integration.
"""
from __future__ import annotations

from typing import Any

from shared.brokers.base import BrokerExecutor


class ZerodhaExecutor(BrokerExecutor):
    """Zerodha (Kite) — stub, not yet implemented."""

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Zerodha executor is not yet implemented")

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        raise NotImplementedError("Zerodha executor is not yet implemented")

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        raise NotImplementedError("Zerodha executor is not yet implemented")


class AngelExecutor(BrokerExecutor):
    """Angel One (SmartAPI) — stub, not yet implemented."""

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Angel executor is not yet implemented")

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        raise NotImplementedError("Angel executor is not yet implemented")

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        raise NotImplementedError("Angel executor is not yet implemented")


class UpstoxExecutor(BrokerExecutor):
    """Upstox — stub, not yet implemented."""

    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Upstox executor is not yet implemented")

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        raise NotImplementedError("Upstox executor is not yet implemented")

    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        raise NotImplementedError("Upstox executor is not yet implemented")
