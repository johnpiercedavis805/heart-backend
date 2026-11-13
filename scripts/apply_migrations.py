#!/usr/bin/env python3
# HeArt v1 — invented by John Pierce Davis Jr., Inventor and CEO.
"""Apply supabase/migrations/*.sql to the production Postgres database.

Idempotent migration runner: safe to re-run. Applied files are recorded in
the heart_schema_migrations table, so a second run skips everything instead
of failing on migrations 2-4 (which use plain CREATE TABLE, not IF NOT
EXISTS). Migration 1 is already IF NOT EXISTS-safe on its own.

Guards (all must pass or it refuses to run):
  1. DATABASE_URL must be set in the environment — no default, no fallback.
  2. DATABASE_URL must point at Postgres — it will NOT touch the local
     sqlite dev database (dev/test tables come from init_db / create_all).
  3. --yes flag required — no accidental runs.
  4. psycopg installed (the prod driver; the Dockerfile image includes it).

Usage:
    DATABASE_URL='postgresql+psycopg://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres' \\
        python scripts/apply_migrations.py --yes

Do NOT run this against anything but the real production database.
There is no production database yet — this script is written and
syntax-checked only, never executed, until John approves paid infra.
"""

import os
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "supabase" / "migrations"

TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS heart_schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def main() -> int:
    if "--yes" not in sys.argv:
        print("Refusing: pass --yes to apply migrations to production.", file=sys.stderr)
        return 2

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print(
            "Refusing: DATABASE_URL is not set. Set it to the production "
            "Postgres URL (never a default, never sqlite).",
            file=sys.stderr,
        )
        return 2
    if not database_url.startswith("postgresql"):
        print(
            f"Refusing: DATABASE_URL is not Postgres ({database_url.split('://')[0]}). "
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

    applied: set[str] = set()
    print(f"Checking {len(files)} migration(s) against Postgres...")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(TRACKING_DDL)
            cur.execute("SELECT filename FROM heart_schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
        conn.commit()

        for path in files:
            if path.name in applied:
                print(f"  SKIP {path.name} (already applied)")
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)  # whole file, one transaction; aborts cleanly on error
                cur.execute(
                    "INSERT INTO heart_schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
            conn.commit()
            print(f"  OK   {path.name}")
    print("Migrations up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
