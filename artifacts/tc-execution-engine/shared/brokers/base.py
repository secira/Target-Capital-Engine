"""Abstract base class for all broker executors."""
from abc import ABC, abstractmethod
from typing import Any


class BrokerRejectedError(Exception):
    """The broker DEFINITIVELY rejected the order (it reached the broker and
    came back as a structured failure). Safe to mark the order REJECTED — no
    live order exists at the broker."""


class BrokerUnknownStateError(Exception):
    """The broker call did not return a confirmed result — timeout, dropped
    connection, JSON parse error, etc. The order MAY or MAY NOT have been
    placed at the broker. The caller MUST NOT assume rejection; the order
    should be left PENDING and reconciled against the broker before any retry.
    """


class BrokerExecutor(ABC):
    """All broker adapters must implement this interface."""

    def __init__(self, client_id: str, access_token: str) -> None:
        self.client_id = client_id
        self.access_token = access_token

    @abstractmethod
    def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        """Place an order with the broker.

        Returns a dict with at minimum:
          broker_order_id (str), status (str), raw (dict)
        """

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        """Cancel a previously placed order."""

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        """Fetch the current status of an order from the broker."""
