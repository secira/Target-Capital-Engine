"""Create the engine's database schema.

Runs `Base.metadata.create_all()` against DATABASE_URL, creating any of the
following tables that don't yet exist:

    users, broker_account, trading_signal, trade, broker_order

Usage:

    cd artifacts/tc-execution-engine
    python scripts/init_schema.py

NOTE: The default `tc_exec` Postgres role is intentionally READ-ONLY on
`users`, `broker_account`, and `trading_signal`, and has only INSERT/UPDATE on
`trade` and `broker_order`. It therefore CANNOT create tables. Point
DATABASE_URL at a migration/superuser role for this script only — do NOT run
the engine itself with that elevated DSN.

This is idempotent: existing tables are left alone.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect

from shared.models import Base


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL is not set", file=sys.stderr)
        return 1

    engine = create_engine(db_url)
    inspector = inspect(engine)
    before = set(inspector.get_table_names())
    expected = {t.name for t in Base.metadata.sorted_tables}

    print(f"Existing tables: {sorted(before) or '(none)'}")
    print(f"Engine expects:  {sorted(expected)}")

    missing = expected - before
    if not missing:
        print("All expected tables already exist — nothing to do.")
        return 0

    print(f"Creating: {sorted(missing)}")
    try:
        Base.metadata.create_all(engine)
    except Exception as exc:
        print(f"ERROR: create_all failed: {exc}", file=sys.stderr)
        return 2

    after = set(inspect(engine).get_table_names())
    print(f"Tables after: {sorted(after)}")
    print("Schema init complete. Next: `python scripts/seed_test_data.py`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
