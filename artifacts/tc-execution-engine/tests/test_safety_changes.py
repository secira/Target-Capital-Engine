"""Unit tests for the order-safety hardening changes.

Covers:
  * Dhan order-status -> TC enum mapping (incl. unknown -> PENDING)
  * inline-vs-DB broker credential precedence in _resolve_broker_creds
  * HMAC canonical forms: request-line binding vs legacy, and tamper rejection

Run:  pytest tests/test_safety_changes.py
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

def test_status_map_known_values():
    from shared.brokers.dhan import _map_status

    assert _map_status("TRANSIT") == "PENDING"
    assert _map_status("PENDING") == "PENDING"
    assert _map_status("TRADED") == "COMPLETE"
    assert _map_status("PART_TRADED") == "OPEN"
    assert _map_status("REJECTED") == "REJECTED"
    assert _map_status("CANCELLED") == "CANCELLED"
    assert _map_status("EXPIRED") == "CANCELLED"


def test_status_map_unknown_defaults_pending():
    from shared.brokers.dhan import _map_status

    for junk in ("SOMETHING_NEW", "", None, "   "):
        assert _map_status(junk) == "PENDING"


def test_status_map_outputs_are_valid_enum_members():
    from shared.brokers.dhan import _STATUS_MAP, _map_status

    valid = {"PENDING", "OPEN", "COMPLETE", "CANCELLED", "REJECTED"}
    for raw in list(_STATUS_MAP) + ["WHO_KNOWS"]:
        assert _map_status(raw) in valid


# ---------------------------------------------------------------------------
# Credential precedence
# ---------------------------------------------------------------------------

def _fake_ub(**kw):
    base = dict(
        id=48, broker_type="dhan", broker_name="Dhan",
        api_key="ENC_API_KEY", access_token="ENC_TOKEN",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_inline_creds_preferred_over_db(monkeypatch):
    from app.routers import orders

    def _boom(_):
        raise AssertionError("decrypt must NOT be called when inline creds present")

    monkeypatch.setattr(orders, "decrypt", _boom)
    bt, cid, tok = orders._resolve_broker_creds(
        _fake_ub(),
        inline_broker_type="dhan",
        inline_client_id="  1105286586  ",   # whitespace must be stripped
        inline_access_token="  eyJtoken  ",
    )
    assert (bt, cid, tok) == ("dhan", "1105286586", "eyJtoken")


def test_falls_back_to_db_when_no_inline(monkeypatch):
    from app.routers import orders

    monkeypatch.setattr(orders, "decrypt", lambda v: f"dec({v})")
    bt, cid, tok = orders._resolve_broker_creds(_fake_ub())
    assert bt == "dhan"
    assert cid == "dec(ENC_API_KEY)"
    assert tok == "dec(ENC_TOKEN)"


def test_partial_inline_falls_back(monkeypatch):
    # Only a token, no client_id -> must NOT take the inline path.
    from app.routers import orders

    monkeypatch.setattr(orders, "decrypt", lambda v: f"dec({v})")
    _, cid, tok = orders._resolve_broker_creds(
        _fake_ub(), inline_access_token="eyJtoken"
    )
    assert cid == "dec(ENC_API_KEY)" and tok == "dec(ENC_TOKEN)"


# ---------------------------------------------------------------------------
# HMAC canonical forms
# ---------------------------------------------------------------------------

def test_canonical_request_line_sorts_query():
    from app.middleware.hmac_auth import _canonical_request_line

    assert _canonical_request_line("get", "/v1/orders/5", "b=2&a=1") == (
        "GET /v1/orders/5?a=1&b=2"
    )
    assert _canonical_request_line("POST", "/v1/orders", "") == "POST /v1/orders"


def test_bound_signature_differs_per_path():
    from app.middleware.hmac_auth import _compute_hmac_bound

    secret = b"s" * 32
    ts = "1700000000"
    sig_a = _compute_hmac_bound(secret, ts, "GET /v1/orders/5", b"")
    sig_b = _compute_hmac_bound(secret, ts, "GET /v1/orders/6", b"")
    # Tampering the order id (path) must invalidate the signature.
    assert sig_a != sig_b


def test_bound_differs_from_legacy():
    from app.middleware.hmac_auth import _compute_hmac, _compute_hmac_bound

    secret = b"s" * 32
    ts = "1700000000"
    body = b'{"x":1}'
    legacy = _compute_hmac(secret, ts, body)
    bound = _compute_hmac_bound(secret, ts, "POST /v1/orders", body)
    assert legacy != bound


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
