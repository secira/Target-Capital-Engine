"""HMAC verification FastAPI dependency.

Validates:
  X-TC-Signature  — HMAC-SHA256(timestamp + "." + raw_body, EXECUTION_HMAC_SECRET)
  X-TC-Timestamp  — Unix seconds; rejects if |now - ts| > 60s
  X-TC-Request-ID — logged on every line (optional but recommended)
  X-TC-Idempotency — passed through; handled by idempotency middleware
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

from fastapi import Depends, Header, HTTPException, Request

logger = logging.getLogger(__name__)


def _get_secret() -> bytes:
    secret = os.environ.get("EXECUTION_HMAC_SECRET", "")
    if not secret:
        raise RuntimeError("EXECUTION_HMAC_SECRET environment variable is not set")
    return secret.encode()


async def verify_hmac(
    request: Request,
    x_tc_signature: str = Header(..., alias="X-TC-Signature"),
    x_tc_timestamp: str = Header(..., alias="X-TC-Timestamp"),
    x_tc_request_id: str = Header(default="", alias="X-TC-Request-ID"),
) -> str:
    """FastAPI dependency that enforces HMAC-signed requests.

    Returns the request_id for downstream use.
    Raises HTTP 401 on any failure.
    """
    # Attach request_id to the request state for logging
    request.state.request_id = x_tc_request_id or ""

    # 1. Validate timestamp freshness
    try:
        req_time = float(x_tc_timestamp)
    except (ValueError, TypeError):
        logger.warning(
            "HMAC reject — bad timestamp format request_id=%s", x_tc_request_id
        )
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

    # 3. Compute expected signature
    secret = _get_secret()
    message = f"{x_tc_timestamp}.".encode() + raw_body
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()

    # 4. Constant-time comparison
    if not hmac.compare_digest(expected, x_tc_signature.lower()):
        logger.warning(
            "HMAC reject — signature mismatch request_id=%s", x_tc_request_id
        )
        raise HTTPException(status_code=401, detail="Invalid signature")

    logger.debug(
        "HMAC OK request_id=%s timestamp=%s", x_tc_request_id, x_tc_timestamp
    )
    return x_tc_request_id
