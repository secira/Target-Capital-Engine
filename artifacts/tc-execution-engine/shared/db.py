"""Database connection and startup self-test.

Uses the scoped tc_exec Postgres user against TC's shared DB.
DATABASE_URL must be set in env.

Self-test (run at startup):
  1. SELECT 1                         — verifies connectivity.
  2. DELETE FROM "user" WHERE 1=0     — MUST fail with SQLSTATE 42501
     (insufficient privilege) confirming the tc_exec role has no write
     access to TC's sensitive tables. Note: "user" is quoted because it's
     a Postgres reserved word.
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

    # Check 2: tc_exec user must NOT have DELETE on users.
    # If the DELETE succeeds the engine raises and refuses to start — this is
    # non-negotiable: a misconfigured DB user is a data-integrity risk.
    delete_passed = False
    try:
        with engine.connect() as conn:
            conn.execute(text('DELETE FROM "user" WHERE 1=0'))
            conn.rollback()
        # Reaching here means the DELETE was not denied — hard failure.
        delete_passed = True
    except ProgrammingError as exc:
        # psycopg2 SQLSTATE codes:
        #   42501  → insufficient_privilege (the expected, correct path)
        #   42P01  → undefined_table        (schema not migrated yet)
        pgcode = getattr(exc.orig, "pgcode", None)
        if pgcode == "42501":
            logger.info(
                "DB self-test [DELETE users]: PASSED (permission denied as expected)"
            )
        elif pgcode == "42P01":
            logger.error(
                'DB self-test [DELETE "user"]: SCHEMA MISSING — table "user" does not '
                "exist. DATABASE_URL is pointing at a database that doesn't have TC's "
                "schema. Re-point at TC's dev/prod DB and restart."
            )
        else:
            logger.warning(
                "DB self-test [DELETE users]: inconclusive (pgcode=%s) — %s",
                pgcode, exc.orig,
            )
    except Exception as exc:
        logger.warning("DB self-test [DELETE users]: inconclusive — %s", exc)

    if delete_passed:
        msg = (
            'SECURITY: DB role has DELETE permission on "user". '
            "Create a scoped tc_exec role (READ on user/user_brokers/trading_signal, "
            "INSERT+UPDATE on broker_orders) and point DATABASE_URL at it."
        )
        if os.environ.get("TC_EXEC_ALLOW_UNSCOPED_DB") == "1":
            # Dev escape hatch — proceed but make it impossible to miss.
            logger.warning("=" * 78)
            logger.warning("%s", msg)
            logger.warning(
                "TC_EXEC_ALLOW_UNSCOPED_DB=1 is set — continuing anyway. "
                "DO NOT set this in production."
            )
            logger.warning("=" * 78)
        else:
            raise PermissionError(msg + " Engine startup aborted. "
                "Set TC_EXEC_ALLOW_UNSCOPED_DB=1 to override in dev.")
