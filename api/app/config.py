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
    # No default — the admin revocation endpoint must never ship with a
    # publicly-known token. Fail fast at import if it is unset.
    ADMIN_TOKEN: str = os.environ.get("ADMIN_TOKEN", "")

    # Day-one guardrails (§2 of the architecture spec)
    RATE_LIMIT_PER_HOUR: int = _int("RATE_LIMIT_PER_HOUR", 100)
    MAX_FILE_BYTES: int = _int("MAX_FILE_BYTES", 10 * 1024 * 1024)  # 10 MB
    MAX_WORKS_PER_DAY: int = _int("MAX_WORKS_PER_DAY", 1)  # John's call Oct 11: 1/day

    # Key lifecycle (§2)
    KEY_ROTATION_DAYS: int = _int("KEY_ROTATION_DAYS", 90)
    ROTATION_GRACE_HOURS: int = _int("ROTATION_GRACE_HOURS", 24)

    # Registry verification timestamp signer (Ed25519 seed, 64 hex chars).
    # Optional in dev/tests — an ephemeral per-process key is generated when
    # unset. Production should set a stable key and publish its public key
    # out-of-band so verifiers can pin it.
    REGISTRY_SIGNING_KEY: str | None = os.environ.get("REGISTRY_SIGNING_KEY")

    # ---- Stripe billing (HeArt Premium; plan: stripe-integration-plan-2026-11-07)
    # TEST MODE ONLY by default. Keys live in env vars — never in the repo,
    # never in chat. The service refuses a live key while
    # STRIPE_TEST_MODE_ONLY is on (see billing._require_test_mode_key).
    STRIPE_SECRET_KEY: str = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY: str = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    # Flat-rate premium price (created in the Stripe Dashboard, test mode):
    # e.g. price_... — see api/scripts/stripe_create_price.py.
    STRIPE_PRICE_PREMIUM_MONTHLY: str = os.environ.get("STRIPE_PRICE_PREMIUM_MONTHLY", "")
    # Hosted Checkout redirect targets (App-to-Web path). The mobile app
    # should register these as deep links / web fallbacks.
    STRIPE_CHECKOUT_SUCCESS_URL: str = os.environ.get("STRIPE_CHECKOUT_SUCCESS_URL", "")
    STRIPE_CHECKOUT_CANCEL_URL: str = os.environ.get("STRIPE_CHECKOUT_CANCEL_URL", "")
    # Stripe Tax collection: OFF by default (threshold monitoring only).
    # Enable before the FIRST LIVE charge — John's call, not a code change.
    STRIPE_TAX_ENABLED: bool = os.environ.get("STRIPE_TAX_ENABLED", "0") not in ("0", "false", "False")
    STRIPE_TEST_MODE_ONLY: bool = os.environ.get("STRIPE_TEST_MODE_ONLY", "1") not in ("0", "false", "False")


settings = Settings()

if not settings.ADMIN_TOKEN:
    raise RuntimeError(
        "ADMIN_TOKEN environment variable must be set. "
        "There is intentionally no default: the admin key-revocation endpoint "
        "must never be protected by a publicly-known token."
    )
