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
    """LEGACY canonical form: HMAC-SHA256( timestamp + "." + raw_body ).

    Does NOT bind the method/path/query, so a captured GET signature can be
    replayed against a different URL within the timestamp window. Kept only for
    backward-compatibility until callers adopt the bound form below; disable via
    HMAC_STRICT_REQUEST_BINDING=1.
    """
    message = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _compute_hmac_no_separator(secret: bytes, timestamp: str) -> str:
    """LEGACY alt form for empty-body requests (typical GET): HMAC( timestamp )."""
    return hmac.new(secret, timestamp.encode(), hashlib.sha256).hexdigest()


def _canonical_request_line(method: str, path: str, query: str) -> str:
    """Deterministic request line: 'METHOD path' or 'METHOD path?sorted&query'.

    Query pairs are sorted so the signer and verifier agree regardless of
    parameter ordering. Callers must reproduce this exactly.
    """
    if query:
        pairs = "&".join(sorted(query.split("&")))
        return f"{method.upper()} {path}?{pairs}"
    return f"{method.upper()} {path}"


def _compute_hmac_bound(
    secret: bytes, timestamp: str, request_line: str, raw_body: bytes
) -> str:
    """PREFERRED canonical form binding the request line into the signature:
    HMAC-SHA256( timestamp + "." + request_line + "." + raw_body ).

    This prevents replaying a captured signature against a different
    method/path/query — closing the GET parameter-tampering gap.
    """
    message = f"{timestamp}.{request_line}.".encode() + raw_body
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
    # Preserve the request_id that request_id_middleware already assigned
    # (it auto-generates an "auto-…" id when the caller didn't send one).
    # Only overwrite if the caller actually sent X-TC-Request-ID.
    if x_tc_request_id:
        request.state.request_id = x_tc_request_id
    else:
        x_tc_request_id = getattr(request.state, "request_id", "") or ""

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

    # 3. Try each active secret — accept if any matches (constant-time each).
    # For empty-body requests (typical GET), also accept the "no-separator"
    # canonicalization since many HTTP clients/libraries don't include a body
    # placeholder in the signed payload for GETs.
    incoming = x_tc_signature.lower()
    secrets = _get_active_secrets()
    strict = os.environ.get("HMAC_STRICT_REQUEST_BINDING") == "1"
    request_line = _canonical_request_line(
        request.method, request.url.path, request.url.query
    )
    matched = False
    matched_form = ""
    for secret in secrets:
        # Preferred: signature bound to METHOD + path + query + body.
        bound = _compute_hmac_bound(secret, x_tc_timestamp, request_line, raw_body)
        if hmac.compare_digest(bound, incoming):
            matched, matched_form = True, "bound"
            break
        if strict:
            # Strict mode: only the bound form is acceptable.
            continue
        # Legacy compatibility — accepted until callers migrate to the bound
        # form, then disable with HMAC_STRICT_REQUEST_BINDING=1.
        expected = _compute_hmac(secret, x_tc_timestamp, raw_body)
        if hmac.compare_digest(expected, incoming):
            matched, matched_form = True, "legacy"
            break
        if not raw_body:
            alt = _compute_hmac_no_separator(secret, x_tc_timestamp)
            if hmac.compare_digest(alt, incoming):
                matched, matched_form = True, "legacy-nosep"
                break

    if not matched:
        logger.warning("HMAC reject — signature mismatch request_id=%s", x_tc_request_id)
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Promoted to INFO so we get a visible accept/reject pair per request at
    # the default LOG_LEVEL=INFO. `form` shows which canonicalization matched —
    # watch for all traffic moving to "bound" before enabling strict mode.
    logger.info(
        "HMAC OK request_id=%s timestamp=%s body_len=%d form=%s",
        x_tc_request_id, x_tc_timestamp, len(raw_body), matched_form,
    )
    return x_tc_request_id
