"""Fernet-based encryption/decryption for broker credentials.

Uses the same scheme as Target Capital: BROKER_MASTER_KEY is a URL-safe
base64-encoded 32-byte Fernet key stored in env / Replit Secrets.
"""
from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.environ.get("BROKER_MASTER_KEY", "")
    if not key:
        raise RuntimeError("BROKER_MASTER_KEY environment variable is not set")
    # Ensure the key is valid base64-encoded bytes
    try:
        base64.urlsafe_b64decode(key + "==")  # padding-tolerant check
    except Exception as exc:
        raise ValueError("BROKER_MASTER_KEY is not valid base64") from exc
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string and return a base64 token."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token and return the plaintext string."""
    return _get_fernet().decrypt(token.encode()).decode()
