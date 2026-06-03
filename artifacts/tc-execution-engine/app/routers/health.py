"""Health, version, and halt endpoints.

GET  /healthz          — unauthenticated; returns {"status":"ok","version":"<sha>"}
GET  /version          — unauthenticated; returns the deployed git SHA
GET  /v1/halt          — unauthenticated; returns current halt state
PUT  /v1/halt          — requires X-TC-Admin-Token matching ADMIN_TOKEN env var

Halt state is persisted to a SQLite file (halt_state.db) so it survives
restarts without needing a separate Redis/Postgres write.
"""
from __future__ import annotations

import hmac
import logging
import os
import sqlite3
import threading
import time as _time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from shared.schemas import HaltState, SetHaltRequest

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Halt-state in-memory cache (Bug 4 fix)
# Re-reads SQLite at most once per second instead of on every order.
# ---------------------------------------------------------------------------
_halt_cache_lock = threading.Lock()
_halt_cache_value: bool = False
_halt_cache_expires: float = 0.0
_HALT_CACHE_TTL: float = 1.0

# ---------------------------------------------------------------------------
# Git SHA
# ---------------------------------------------------------------------------

def _get_version() -> str:
    """Return the current git SHA, or 'dev' if not available."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return os.environ.get("GIT_SHA", "dev")


_VERSION = _get_version()


# ---------------------------------------------------------------------------
# Halt state — SQLite persistence
# ---------------------------------------------------------------------------

_HALT_DB = os.environ.get("HALT_DB_PATH", "halt_state.db")


def _get_halt_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_HALT_DB, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS halt_state (id INTEGER PRIMARY KEY, halted INTEGER NOT NULL DEFAULT 0, reason TEXT)"
    )
    conn.commit()
    return conn


def _read_halt() -> HaltState:
    with _get_halt_conn() as conn:
        row = conn.execute("SELECT halted, reason FROM halt_state WHERE id=1").fetchone()
        if row is None:
            return HaltState(halted=False)
        return HaltState(halted=bool(row[0]), reason=row[1])


def _write_halt(state: HaltState) -> None:
    with _get_halt_conn() as conn:
        conn.execute(
            "INSERT INTO halt_state (id, halted, reason) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET halted=excluded.halted, reason=excluded.reason",
            (1 if state.halted else 0, state.reason),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/healthz", tags=["health"])
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": _VERSION})


@router.get("/version", tags=["health"])
async def version() -> JSONResponse:
    return JSONResponse({"version": _VERSION})


@router.get("/v1/halt", tags=["halt"])
async def get_halt() -> HaltState:
    return _read_halt()


@router.put("/v1/halt", tags=["halt"])
async def set_halt(
    body: SetHaltRequest,
    x_tc_admin_token: str = Header(..., alias="X-TC-Admin-Token"),
) -> HaltState:
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured")
    if not hmac.compare_digest(x_tc_admin_token, admin_token):
        raise HTTPException(status_code=401, detail="Invalid admin token")

    state = HaltState(halted=body.halted, reason=body.reason)
    _write_halt(state)
    _invalidate_halt_cache()
    logger.info("Halt state set to %s reason=%r", state.halted, state.reason)
    return state


def is_halted() -> bool:
    """Check current halt state — called by order router before broker calls.

    Reads SQLite at most once per second; between reads the cached boolean is
    returned without any I/O (Bug 4 fix).
    """
    global _halt_cache_value, _halt_cache_expires
    now = _time.monotonic()
    if now < _halt_cache_expires:
        return _halt_cache_value
    with _halt_cache_lock:
        if now < _halt_cache_expires:   # double-checked under lock
            return _halt_cache_value
        state = _read_halt()
        _halt_cache_value = state.halted
        _halt_cache_expires = now + _HALT_CACHE_TTL
        return _halt_cache_value


def _invalidate_halt_cache() -> None:
    """Force the next is_halted() call to re-read SQLite (called after a write)."""
    global _halt_cache_expires
    with _halt_cache_lock:
        _halt_cache_expires = 0.0
