"""Verify integration-test row exists in TC's DB.

The engine itself is read-only on `user` and `user_brokers`, so we can't
seed. This script just confirms a known user + user_brokers row exists and
that its encrypted access_token decrypts cleanly with BROKER_MASTER_KEY.

Usage:

    cd artifacts/tc-execution-engine
    python scripts/check_test_data.py [user_id] [user_broker_id]

Defaults: user_id=27, user_broker_id=48 (TC dev DB known-good row).

Env vars required:
    DATABASE_URL         — tc_exec DSN (READ is enough)
    BROKER_MASTER_KEY    — Fernet key, must match TC's
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.crypto import decrypt
from shared.models import User, UserBroker


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL is not set", file=sys.stderr); return 1
    if not os.environ.get("BROKER_MASTER_KEY"):
        print("ERROR: BROKER_MASTER_KEY is not set", file=sys.stderr); return 1

    user_id = int(sys.argv[1]) if len(sys.argv) > 1 else 27
    user_broker_id = int(sys.argv[2]) if len(sys.argv) > 2 else 48

    eng = create_engine(os.environ["DATABASE_URL"])
    session = sessionmaker(bind=eng)()
    rc = 0
    try:
        u = session.query(User).filter(User.id == user_id).first()
        if u is None:
            print(f"FAIL: user id={user_id} not found"); return 2
        print(f"OK   user           id={u.id} username={u.username!r} active={u.active}")

        ub = session.query(UserBroker).filter(UserBroker.id == user_broker_id).first()
        if ub is None:
            print(f"FAIL: user_brokers id={user_broker_id} not found"); return 2
        if ub.user_id != user_id:
            print(f"FAIL: user_brokers {user_broker_id} belongs to user {ub.user_id}, not {user_id}")
            rc = 2
        print(f"OK   user_brokers   id={ub.id} user_id={ub.user_id} "
              f"broker_type={ub.broker_type!r} is_active={ub.is_active} "
              f"tenant_id={ub.tenant_id!r}")

        # Decrypt check — don't print plaintext, just lengths
        for col in ("api_key", "access_token"):
            val = getattr(ub, col)
            if not val:
                print(f"WARN {col} is empty on user_brokers {ub.id}")
                continue
            try:
                pt = decrypt(val)
                print(f"OK   decrypt({col})  → plaintext_len={len(pt)}")
            except Exception as exc:
                print(f"FAIL decrypt({col}) — {exc}")
                rc = 3
        return rc
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
