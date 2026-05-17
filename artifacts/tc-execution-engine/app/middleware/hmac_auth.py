"""HMAC verification FastAPI dependency.

Validates:
  X-TC-Signature  — HMAC-SHA256(timestamp + "." + raw_body, secret)
  X-TC-Timestamp  — Unix seconds; rejects if |now - ts| > 60s
  X-TC-Request-ID — logged on every line (optional but recommended)
  X-TC-Idempotency — passed through; handled by idempotency middleware

Secret rotation
---------------
During a rotation window, set both env vars:
  EXECUTION_HMAC_SECRET      — current (outgoing) key
  EXECUTION_HMAC_SECRET_NEXT — next (incoming) key

The dependency accepts a signature that validates against EITHER key.
Once all callers have switched to the new key, delete EXECUTION_HMAC_SECRET
and rename EXECUTION_HMAC_SECRET_NEXT → EXECUTION_HMAC_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)


def _get_active_secrets() -> list[bytes]:
    """Return a list of 1–2 active HMAC secrets.

    Always includes EXECUTION_HMAC_SECRET (required).
    Appends EXECUTION_HMAC_SECRET_NEXT when set (rotation window).
    """
    current = os.environ.get("EXECUTION_HMAC_SECRET", "")
    if not current:
        raise RuntimeError("EXECUTION_HMAC_SECRET environment variable is not set")
    secrets = [current.encode()]

    next_secret = os.environ.get("EXECUTION_HMAC_SECRET_NEXT", "")
    if next_secret:
        secrets.append(next_secret.encode())
        logger.debug("HMAC rotation window active — accepting both current and next secret")

    return secrets


def _compute_hmac(secret: bytes, timestamp: str, raw_body: bytes) -> str:
    message = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


async def verify_hmac(
    request: Request,
    x_tc_signature: str = Header(..., alias="X-TC-Signature"),
    x_tc_timestamp: str = Header(..., alias="X-TC-Timestamp"),
    x_tc_request_id: str = Header(default="", alias="X-TC-Request-ID"),
) -> str:
    """FastAPI dependency that enforces HMAC-signed requests.

    Accepts a signature valid against any active secret (supports rotation).
    Returns the request_id for downstream use.
    Raises HTTP 401 on any failure.
    """
    request.state.request_id = x_tc_request_id or ""

    # 1. Validate timestamp freshness
    try:
        req_time = float(x_tc_timestamp)
    except (ValueError, TypeError):
        logger.warning("HMAC reject — bad timestamp format request_id=%s", x_tc_request_id)
        raise HTTPException(status_code=401, detail="Invalid X-TC-Timestamp format")

    delta = abs(time.time() - req_time)
    if delta > 60:
        logger.warning(
            "HMAC reject — timestamp too old/future (delta=%.1fs) request_id=%s",
            delta,
            x_tc_request_id,
        )
        raise HTTPException(status_code=401, detail="Request timestamp out of window (±60s)")

    # 2. Read raw body
    raw_body = await request.body()

    # 3. Try each active secret — accept if any matches (constant-time each)
    incoming = x_tc_signature.lower()
    secrets = _get_active_secrets()
    matched = False
    for secret in secrets:
        expected = _compute_hmac(secret, x_tc_timestamp, raw_body)
        if hmac.compare_digest(expected, incoming):
            matched = True
            break

    if not matched:
        logger.warning("HMAC reject — signature mismatch request_id=%s", x_tc_request_id)
        raise HTTPException(status_code=401, detail="Invalid signature")

    logger.debug("HMAC OK request_id=%s timestamp=%s", x_tc_request_id, x_tc_timestamp)
    return x_tc_request_id
