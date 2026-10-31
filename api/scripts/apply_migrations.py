#!/usr/bin/env python3
# HeArt v1 — invented by John Davis.
"""Apply supabase/migrations/*.sql to the production Postgres database.

Free, no-op-safe migration runner — the "no manual SQL in prod" piece.
Reads DATABASE_URL from the environment, runs the 4 migration files in
filename order, whole file per execute (psycopg handles multi-statement).

Guards (all three must pass or it refuses to run):
  1. DATABASE_URL must point at Postgres — it will NOT touch the local
     sqlite dev database (dev/test tables come from init_db / create_all).
  2. --yes flag required — no accidental runs.
  3. psycopg installed (the prod driver; it's in the Dockerfile image).

Usage:
    DATABASE_URL='postgresql+psycopg://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres' \\
        python api/scripts/apply_migrations.py --yes
"""

import os
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def main() -> int:
    if "--yes" not in sys.argv:
        print("Refusing: pass --yes to apply migrations to production.", file=sys.stderr)
        return 2

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        print(
            f"Refusing: DATABASE_URL is not Postgres ({database_url.split('://')[0] or 'unset'}). "
            "Local sqlite dev DB is managed by init_db (create_all), not migrations.",
            file=sys.stderr,
        )
        return 2

    try:
        import psycopg
    except ImportError:
        print(
            "Refusing: psycopg is not installed. Install it with "
            'pip install "psycopg[binary]" (the Dockerfile already does).',
            file=sys.stderr,
        )
        return 2

    # psycopg wants a plain libpq DSN; strip SQLAlchemy's +driver suffix.
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"No migration files found in {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    print(f"Applying {len(files)} migration(s) to Postgres...")
    with psycopg.connect(dsn) as conn:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            print(f"  OK  {path.name}")
    print("All migrations applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
