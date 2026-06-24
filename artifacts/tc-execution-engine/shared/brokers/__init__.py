"""Broker factory — picks the right executor from user_brokers.broker_type."""
from __future__ import annotations

import logging

from shared.brokers.base import (
    BrokerExecutor,
    BrokerRejectedError,
    BrokerUnknownStateError,
)
from shared.brokers.dhan import DhanExecutor
from shared.brokers.stubs import AngelExecutor, UpstoxExecutor, ZerodhaExecutor

logger = logging.getLogger(__name__)


_REGISTRY: dict[str, type[BrokerExecutor]] = {
    "dhan": DhanExecutor,
    "zerodha": ZerodhaExecutor,
    "angel": AngelExecutor,
    "angelone": AngelExecutor,        # tolerate alternate spellings
    "upstox": UpstoxExecutor,
}


class UnsupportedBrokerError(Exception):
    """Raised when broker_type is not in the registry.

    Different from NotImplementedError (which the stubs raise) because this
    is a config/data problem on the caller side, not a Phase-1 limitation.
    """


def supported_brokers() -> list[str]:
    """Return all known broker_type keys (for diagnostics / docs)."""
    return sorted(set(_REGISTRY.keys()))


def get_executor(broker_type: str, client_id: str, access_token: str) -> BrokerExecutor:
    """Return an initialised executor for *broker_type*.

    Raises:
        UnsupportedBrokerError: if broker_type is not recognised.
        NotImplementedError:    if the broker is registered but stubbed
                                (raised by the executor methods, not __init__).
    """
    key = (broker_type or "").lower().strip()
    if not key:
        logger.error("Broker factory called with empty broker_type")
        raise UnsupportedBrokerError("broker_type is empty")
    if key not in _REGISTRY:
        logger.error(
            "Broker factory: unknown broker_type=%r known=%s",
            broker_type, supported_brokers(),
        )
        raise UnsupportedBrokerError(
            f"Unknown broker type: {broker_type!r}. "
            f"Supported: {supported_brokers()}"
        )
    cls = _REGISTRY[key]
    logger.debug(
        "Broker factory: dispatching broker_type=%r → %s client_id=%s",
        broker_type, cls.__name__, _mask(client_id),
    )
    return cls(client_id=client_id, access_token=access_token)


def _mask(s: str, keep: int = 4) -> str:
    """Mask a credential for logging (`12345678` → `1234****`)."""
    if not s:
        return "<empty>"
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep)


__all__ = [
    "BrokerExecutor",
    "UnsupportedBrokerError",
    "BrokerRejectedError",
    "BrokerUnknownStateError",
    "get_executor",
    "supported_brokers",
]
