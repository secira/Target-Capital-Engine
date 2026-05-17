"""Broker factory — picks the right executor from broker_account.broker_type."""
from shared.brokers.base import BrokerExecutor
from shared.brokers.dhan import DhanExecutor
from shared.brokers.stubs import ZerodhaExecutor, AngelExecutor, UpstoxExecutor


_REGISTRY: dict[str, type[BrokerExecutor]] = {
    "dhan": DhanExecutor,
    "zerodha": ZerodhaExecutor,
    "angel": AngelExecutor,
    "upstox": UpstoxExecutor,
}


def get_executor(broker_type: str, client_id: str, access_token: str) -> BrokerExecutor:
    """Return an initialised executor for *broker_type*.

    Raises:
        KeyError: if broker_type is not recognised.
        NotImplementedError: if the broker is stubbed.
    """
    key = broker_type.lower()
    if key not in _REGISTRY:
        raise KeyError(f"Unknown broker type: {broker_type!r}")
    return _REGISTRY[key](client_id=client_id, access_token=access_token)


__all__ = ["get_executor", "BrokerExecutor"]
