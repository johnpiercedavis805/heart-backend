# HeArt v1 API — invented by John Davis.
"""Runtime configuration. Everything is overridable via environment variables."""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Settings:
    # sqlite for local dev; postgres for Supabase/prod, e.g.
    # postgresql+psycopg://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./heart.db")
    UPLOAD_DIR: str = os.environ.get("UPLOAD_DIR", "./uploads")
    ADMIN_TOKEN: str = os.environ.get("ADMIN_TOKEN", "dev-admin-token-change-me")

    # Day-one guardrails (§2 of the architecture spec)
    RATE_LIMIT_PER_HOUR: int = _int("RATE_LIMIT_PER_HOUR", 100)
    MAX_FILE_BYTES: int = _int("MAX_FILE_BYTES", 10 * 1024 * 1024)  # 10 MB
    MAX_WORKS_PER_DAY: int = _int("MAX_WORKS_PER_DAY", 1)  # John's call Oct 11: 1/day

    # Key lifecycle (§2)
    KEY_ROTATION_DAYS: int = _int("KEY_ROTATION_DAYS", 90)
    ROTATION_GRACE_HOURS: int = _int("ROTATION_GRACE_HOURS", 24)


settings = Settings()
