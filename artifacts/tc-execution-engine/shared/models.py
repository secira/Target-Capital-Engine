"""SQLAlchemy ORM models — bound to Target Capital's real schema.

The engine has NO database of its own. It reads/writes the TC Postgres
instance using the scoped `tc_exec` role:

  READ  : "user", user_brokers, trading_signal
  WRITE : broker_orders (INSERT + UPDATE only)

Key facts about the TC schema that drove these mappings:
  - Primary keys are INTEGER, not UUID.
  - The user table is singular, quoted: "user" (reserved word in Postgres).
  - `user_brokers` is the broker connection table. Its `api_key`,
    `access_token`, and `api_secret` columns are Fernet-encrypted text using
    BROKER_ENCRYPTION_KEY. For Dhan, api_key holds the client_id and
    access_token holds the token.
  - `broker_orders.broker_account_id` is confusingly named: it actually
    references user_brokers(id), NOT broker_accounts(id). TC's
    broker_accounts table exists but is empty/deprecated — do not use it.
  - There is NO intermediate `trade` table. The engine writes directly to
    broker_orders.
  - `correlation_id` on broker_orders is used as the idempotency key
    (X-TC-Idempotency from the caller).
"""
from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """TC's `user` table (singular, reserved word — must be quoted)."""

    __tablename__ = "user"

    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False)
    email = Column(String)
    # TC uses `active` (not `is_active`)
    active = Column(Boolean)
    tenant_id = Column(String)

class UserBroker(Base):
    """TC's `user_brokers` table — the broker connection row.

    api_key, access_token, api_secret are Fernet-encrypted text. Call
    `shared.crypto.decrypt(value)` before handing to the broker SDK.

    For Dhan: api_key = client_id, access_token = bearer token.
    """

    __tablename__ = "user_brokers"

    id = Column(Integer, primary_key=True)
    # NB: don't quote "user" inside the FK string — SQLAlchemy auto-quotes
    # reserved words when it emits SQL against the Table object.
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    broker_name = Column(String, nullable=False)        # human label, e.g. "Dhan"
    broker_type = Column(String)                         # lowercase: "dhan", "zerodha", ...
    api_key = Column(Text)                               # Fernet-encrypted
    api_secret = Column(Text)                            # Fernet-encrypted
    access_token = Column(Text)                          # Fernet-encrypted
    is_active = Column(Boolean)
    is_primary = Column(Boolean)
    connection_status = Column(String)
    tenant_id = Column(String)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    user = relationship("User", foreign_keys=[user_id], lazy="joined")
    orders = relationship(
        "BrokerOrder", back_populates="user_broker",
        foreign_keys="BrokerOrder.broker_account_id",
    )


class TradingSignal(Base):
    """TC's `trading_signal` table — read-only reference."""

    __tablename__ = "trading_signal"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    action = Column(String, nullable=False)              # "BUY" / "SELL"
    tenant_id = Column(String)
    created_at = Column(DateTime)


class BrokerOrder(Base):
    """TC's `broker_orders` table — where the engine writes orders.

    Despite the column name, `broker_account_id` references user_brokers(id).
    """

    __tablename__ = "broker_orders"

    id = Column(Integer, primary_key=True)
    broker_account_id = Column(
        Integer, ForeignKey("user_brokers.id"), nullable=False
    )
    broker_order_id = Column(String)                     # set after broker accepts
    correlation_id = Column(String)                       # holds X-TC-Idempotency
    symbol = Column(String, nullable=False)
    trading_symbol = Column(String, nullable=False)
    exchange = Column(String, nullable=False)            # e.g. NSE_EQ
    security_id = Column(String)

    # Postgres enums — values must match exactly (case-sensitive):
    #   transactiontype: BUY / SELL
    #   ordertype:       MARKET / LIMIT / SL / SL_M
    #   producttype:     INTRADAY / DELIVERY / CNC / MIS
    #   orderstatus:     PENDING / OPEN / COMPLETE / CANCELLED / REJECTED
    transaction_type = Column(String, nullable=False)
    order_type = Column(String, nullable=False)
    product_type = Column(String, nullable=False)
    order_status = Column(String)

    quantity = Column(Integer, nullable=False)
    filled_quantity = Column(Integer)
    pending_quantity = Column(Integer)
    price = Column(Float)
    trigger_price = Column(Float)
    disclosed_quantity = Column(Integer)
    status_message = Column(String)
    avg_execution_price = Column(Float)

    trading_signal_id = Column(Integer, ForeignKey("trading_signal.id"))
    tenant_id = Column(String, nullable=False)           # FK to tenants

    order_time = Column(DateTime, default=datetime.datetime.utcnow)
    execution_time = Column(DateTime)
    last_updated = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user_broker = relationship(
        "UserBroker", back_populates="orders",
        foreign_keys=[broker_account_id], lazy="joined",
    )
    signal = relationship("TradingSignal", foreign_keys=[trading_signal_id])
