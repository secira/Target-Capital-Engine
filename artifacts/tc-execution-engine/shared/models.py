"""SQLAlchemy ORM models for the tc-execution-engine.

These mirror the Target Capital schema.  The tc_exec Postgres user has:
  READ:   users, broker_account, trading_signal
  WRITE:  trade, broker_order  (INSERT / UPDATE only)

Do NOT add INSERT/UPDATE columns for read-only tables here.
"""
from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    broker_accounts = relationship("BrokerAccount", back_populates="user")
    trades = relationship("Trade", back_populates="user")


class BrokerAccount(Base):
    __tablename__ = "broker_account"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    broker_type = Column(String(50), nullable=False)  # dhan, zerodha, angel, upstox
    client_id = Column(String(255), nullable=False)
    # Encrypted with Fernet using BROKER_MASTER_KEY
    encrypted_access_token = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user = relationship("User", back_populates="broker_accounts")
    trades = relationship("Trade", back_populates="broker_account")

    __table_args__ = (
        UniqueConstraint("user_id", "broker_type", name="uq_user_broker"),
    )


class TradingSignal(Base):
    __tablename__ = "trading_signal"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    signal_type = Column(String(20), nullable=False)  # BUY, SELL
    quantity = Column(Integer, nullable=False)
    price = Column(Numeric(12, 4))
    status = Column(String(30), default="pending")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    trades = relationship("Trade", back_populates="signal")


class Trade(Base):
    __tablename__ = "trade"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    broker_account_id = Column(UUID(as_uuid=True), ForeignKey("broker_account.id"), nullable=False)
    signal_id = Column(UUID(as_uuid=True), ForeignKey("trading_signal.id"), nullable=True)

    symbol = Column(String(50), nullable=False)
    exchange = Column(String(20), nullable=False)
    transaction_type = Column(String(10), nullable=False)  # BUY / SELL
    quantity = Column(Integer, nullable=False)
    price = Column(Numeric(12, 4))
    order_type = Column(String(20), nullable=False)
    product_type = Column(String(20), nullable=False)

    status = Column(String(30), default="pending")  # pending, placed, filled, rejected, cancelled
    error_code = Column(String(50))
    error_message = Column(Text)

    idempotency_key = Column(String(255), unique=True, nullable=True)
    request_id = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user = relationship("User", back_populates="trades")
    broker_account = relationship("BrokerAccount", back_populates="trades")
    signal = relationship("TradingSignal", back_populates="trades")
    broker_orders = relationship("BrokerOrder", back_populates="trade")


class BrokerOrder(Base):
    __tablename__ = "broker_order"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id = Column(UUID(as_uuid=True), ForeignKey("trade.id"), nullable=False)
    broker_type = Column(String(50), nullable=False)
    broker_order_id = Column(String(255), nullable=False)
    status = Column(String(30), default="pending")

    # Raw JSON response from broker stored as text
    raw_request = Column(Text)
    raw_response = Column(Text)

    filled_quantity = Column(Integer, default=0)
    average_price = Column(Numeric(12, 4))

    placed_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    trade = relationship("Trade", back_populates="broker_orders")
