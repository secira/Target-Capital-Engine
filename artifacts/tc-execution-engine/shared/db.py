"""Database connection and startup self-test.

Uses the scoped tc_exec Postgres user.  DATABASE_URL must be set in env.

Self-test (run at startup):
  1. SELECT 1  — verifies connectivity.
  2. DELETE FROM users LIMIT 1  — MUST fail (permission denied) confirming
     the tc_exec user has no write access to sensitive tables.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import ProgrammingError, OperationalError

logger = logging.getLogger(__name__)

_DATABASE_URL: str | None = None
_engine = None
_SessionLocal: sessionmaker | None = None


def _get_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return url


def get_engine():
    global _engine, _DATABASE_URL
    url = _get_url()
    if _engine is None or url != _DATABASE_URL:
        _DATABASE_URL = url
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autocommit=False, autoflush=False
        )
    return _SessionLocal


def get_db() -> Session:
    """FastAPI dependency — yields a DB session and closes it afterwards."""
    factory = get_session_factory()
    db: Session = factory()
    try:
        yield db
    finally:
        db.close()


def run_startup_self_test() -> None:
    """Run at application startup.  Logs pass/fail for each check."""
    engine = get_engine()

    # Check 1: basic connectivity
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("DB self-test [SELECT 1]: PASSED")
    except (OperationalError, Exception) as exc:
        logger.error("DB self-test [SELECT 1]: FAILED — %s", exc)
        raise

    # Check 2: tc_exec user must NOT have DELETE on users
    try:
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM users WHERE 1=0"))
            conn.rollback()
        logger.error(
            "DB self-test [DELETE users]: FAILED — tc_exec user has DELETE on users! "
            "Revoke immediately."
        )
    except ProgrammingError as exc:
        # expected: permission denied
        logger.info(
            "DB self-test [DELETE users]: PASSED (permission denied as expected): %s",
            exc.orig,
        )
    except Exception as exc:
        logger.warning(
            "DB self-test [DELETE users]: could not confirm permission — %s", exc
        )
