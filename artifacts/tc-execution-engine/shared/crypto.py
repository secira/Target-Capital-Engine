"""Fernet-based encryption/decryption for broker credentials.

Mirrors Target Capital's resolution chain (from TC's models_broker.py /
security.environment_config.setup_secure_environment):

  1. BROKER_ENCRYPTION_KEY env var (preferred — same value TC uses in prod).
  2. Legacy dev key derived from the literal
     "Target Capital_Dev_Key_32_Chars_Long_123=", padded/truncated to 32
     bytes and url-safe base64 encoded. Dev-only.

`decrypt()` tries each candidate key in order and returns the first one
that successfully decrypts the token. `encrypt()` always uses the first
available key. If no candidate works, the underlying Fernet exception
(typically `InvalidToken`) bubbles up.
"""
from __future__ import annotations

import base64
import os
from typing import List

from cryptography.fernet import Fernet, InvalidToken

_LEGACY_DEV_SEED = b"Target Capital_Dev_Key_32_Chars_Long_123="


def _candidate_keys() -> List[bytes]:
    keys: List[bytes] = []
    env = os.environ.get("BROKER_ENCRYPTION_KEY", "")
    if env:
        env_bytes = env.encode() if isinstance(env, str) else env
        try:
            Fernet(env_bytes)
            keys.append(env_bytes)
        except Exception:
            pass

    # Legacy dev fallback — GATED behind TC_EXEC_ALLOW_UNSCOPED_DB so it never
    # silently runs in production. (Same flag we use to allow an unscoped DB
    # role; both are dev-only escape hatches.)
    if os.environ.get("TC_EXEC_ALLOW_UNSCOPED_DB") == "1":
        seed = _LEGACY_DEV_SEED[:32].ljust(32, b"\x00")
        keys.append(base64.urlsafe_b64encode(seed))
    return keys


def _fernets() -> List[Fernet]:
    out: List[Fernet] = []
    for k in _candidate_keys():
        try:
            out.append(Fernet(k))
        except Exception:
            continue
    if not out:
        raise RuntimeError(
            "No usable Fernet key found. Set BROKER_ENCRYPTION_KEY to TC's master key."
        )
    return out


def encrypt(plaintext: str) -> str:
    """Encrypt with the first (highest-priority) available key."""
    return _fernets()[0].encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Try each candidate key until one decrypts the token.

    Raises the last InvalidToken if none of them work, with a message that
    includes how many keys were tried — InvalidToken itself has an empty
    string repr which is awful to debug.
    """
    keys = _fernets()
    last_exc: Exception = InvalidToken("no keys available")
    for f in keys:
        try:
            return f.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            last_exc = exc
            continue
    raise InvalidToken(
        f"None of the {len(keys)} configured key(s) could decrypt the token. "
        f"Check that BROKER_ENCRYPTION_KEY matches the value used by Target "
        f"Capital when the credential was encrypted."
    ) from last_exc
