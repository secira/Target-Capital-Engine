"""Seed deterministic test rows for integration testing from Target Capital.

Creates (or refreshes):
  - One test user with a known UUID
  - One Dhan broker_account for that user with a known UUID, encrypted using
    BROKER_MASTER_KEY (so the engine can decrypt at order time)

After running, set these in the Target Capital Repl so its execution_proxy
sends UUIDs the engine recognises:

    TEST_USER_ID            = 11111111-1111-1111-1111-111111111111
    TEST_BROKER_ACCOUNT_ID  = 22222222-2222-2222-2222-222222222222

Run from the engine workspace root:

    cd artifacts/tc-execution-engine
    python scripts/seed_test_data.py

Env vars required:
    DATABASE_URL         — Postgres DSN (write access to users + broker_account)
    BROKER_MASTER_KEY    — Fernet key used to encrypt the access token

Optional:
    DHAN_CLIENT_ID       — defaults to "TEST_CLIENT_ID"
    DHAN_ACCESS_TOKEN    — defaults to a placeholder; replace before real orders

NOTE: The default tc_exec Postgres user is READ-ONLY on `users` and
`broker_account`. This script needs a Postgres user with INSERT/UPDATE on
those tables. Set DATABASE_URL to a superuser/migration role for this script
only — do NOT run the engine with that elevated DSN.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

# Make the engine's `shared/` package importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.crypto import encrypt
from shared.models import BrokerAccount, User


# Deterministic UUIDs so Target Capital can hardcode them
TEST_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEST_BROKER_ACCOUNT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        return 1
    if not os.environ.get("BROKER_MASTER_KEY"):
        print("ERROR: BROKER_MASTER_KEY is not set", file=sys.stderr)
        return 1

    client_id = os.environ.get("DHAN_CLIENT_ID", "TEST_CLIENT_ID")
    access_token = os.environ.get(
        "DHAN_ACCESS_TOKEN",
        "PLACEHOLDER_TOKEN_REPLACE_BEFORE_REAL_ORDERS",
    )

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # ---- User ---------------------------------------------------------
        user = session.query(User).filter(User.id == TEST_USER_ID).first()
        if user is None:
            user = User(
                id=TEST_USER_ID,
                email="tc-integration-test@example.com",
                name="TC Integration Test User",
                is_active=True,
            )
            session.add(user)
            print(f"+ created user {TEST_USER_ID}")
        else:
            user.is_active = True
            print(f"= user {TEST_USER_ID} already exists (ensured active)")

        # ---- Broker account ----------------------------------------------
        acct = (
            session.query(BrokerAccount)
            .filter(BrokerAccount.id == TEST_BROKER_ACCOUNT_ID)
            .first()
        )
        encrypted = encrypt(access_token)
        if acct is None:
            acct = BrokerAccount(
                id=TEST_BROKER_ACCOUNT_ID,
                user_id=TEST_USER_ID,
                broker_type="dhan",
                client_id=client_id,
                encrypted_access_token=encrypted,
                is_active=True,
            )
            session.add(acct)
            print(f"+ created broker_account {TEST_BROKER_ACCOUNT_ID} (dhan, client_id={client_id})")
        else:
            acct.user_id = TEST_USER_ID
            acct.broker_type = "dhan"
            acct.client_id = client_id
            acct.encrypted_access_token = encrypted
            acct.is_active = True
            print(f"= broker_account {TEST_BROKER_ACCOUNT_ID} refreshed (client_id={client_id})")

        session.commit()
        print()
        print("Seed complete. Set these in the Target Capital Repl:")
        print(f"  TEST_USER_ID           = {TEST_USER_ID}")
        print(f"  TEST_BROKER_ACCOUNT_ID = {TEST_BROKER_ACCOUNT_ID}")
        return 0
    except Exception as exc:
        session.rollback()
        print(f"ERROR: seed failed: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
