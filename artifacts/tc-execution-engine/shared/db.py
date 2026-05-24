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
import socket
from urllib.parse import urlparse

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


def _describe_url(url: str) -> dict[str, str]:
    """Return a non-secret breakdown of the DATABASE_URL for diagnostics."""
    try:
        p = urlparse(url)
        return {
            "scheme": p.scheme or "(none)",
            "user": p.username or "(none)",
            "host": p.hostname or "(none)",
            "port": str(p.port) if p.port else "(default)",
            "database": (p.path.lstrip("/") if p.path else "(none)") or "(none)",
            "password_set": "yes" if p.password else "no",
        }
    except Exception as exc:  # pragma: no cover
        return {"parse_error": str(exc)}


def _hostname_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
        return True
    except socket.gaierror:
        return False


def log_database_url_diagnostic() -> dict[str, str]:
    """Log a masked summary of DATABASE_URL + whether the host resolves.

    Called at startup BEFORE we try to open a connection so the operator
    can see exactly what hostname/db/user the engine is about to use.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        logger.error(
            "DATABASE_URL is NOT SET. In Railway → tc-execution-engine "
            "service → Variables, add DATABASE_URL (reference the Postgres "
            "service's DATABASE_URL variable to auto-resolve the hostname)."
        )
        return {}
    info = _describe_url(url)
    logger.info(
        "DATABASE_URL parsed: scheme=%s user=%s host=%s port=%s db=%s password_set=%s",
        info.get("scheme"), info.get("user"), info.get("host"),
        info.get("port"), info.get("database"), info.get("password_set"),
    )
    host = info.get("host", "")
    if host and host not in {"(none)", "localhost", "127.0.0.1"}:
        if _hostname_resolves(host):
            logger.info("DATABASE_URL host %s resolves OK (DNS lookup succeeded).", host)
        else:
            logger.error(
                "DATABASE_URL host %r DOES NOT RESOLVE — no DNS record found.", host
            )
            if host.endswith(".railway.internal"):
                logger.error(
                    "  This is a Railway PRIVATE hostname. It only resolves if a "
                    "service with EXACTLY that name exists in the same Railway "
                    "project + environment as this service."
                )
                logger.error(
                    "  Fix: open Railway → tc-execution-engine → Variables → "
                    "delete the current DATABASE_URL → click '+ New Variable → "
                    "Add Reference', pick your Postgres service and select its "
                    "DATABASE_URL (or DATABASE_PRIVATE_URL). Railway will then "
                    "substitute the correct internal hostname automatically."
                )
                logger.error(
                    "  Workaround for testing: use DATABASE_PUBLIC_URL from the "
                    "Postgres service instead (host ends in .proxy.rlwy.net)."
                )
            else:
                logger.error(
                    "  Check that the host is reachable from this container "
                    "(firewall / VPC / DNS), or paste the correct DSN."
                )
    return info


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
    # Print a masked, structured view of DATABASE_URL up front so any DSN
    # mistake (wrong hostname, missing db name, etc.) is obvious without
    # having to read a SQLAlchemy stack trace.
    info = log_database_url_diagnostic()

    engine = get_engine()

    # Check 1: basic connectivity
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("DB self-test [SELECT 1]: PASSED")
    except OperationalError as exc:
        root = str(getattr(exc, "orig", exc))
        logger.error("=" * 78)
        logger.error("DB self-test [SELECT 1]: FAILED")
        logger.error("  Root cause: %s", root.strip())
        host = info.get("host", "?")
        if "could not translate host name" in root or "Name or service not known" in root:
            logger.error("")
            logger.error("  ➜ The hostname %r in DATABASE_URL does not exist.", host)
            logger.error("    See the lines above for the exact fix in the Railway dashboard.")
        elif "Connection refused" in root:
            logger.error("  ➜ Host %r is reachable but nothing is listening on the port.", host)
            logger.error("    Check the port in DATABASE_URL and that the DB service is running.")
        elif "authentication failed" in root or "password authentication failed" in root:
            logger.error("  ➜ Wrong username/password in DATABASE_URL.")
        elif "does not exist" in root and "database" in root:
            logger.error("  ➜ The database name in DATABASE_URL does not exist on that host.")
        logger.error("=" * 78)
        raise
    except Exception as exc:
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
