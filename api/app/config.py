# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Runtime configuration. Everything is overridable via environment variables."""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0") not in ("0", "false", "False")


# DATABASE_URL — T2 red-pen item 2: a missing value used to silently fall
# back to local SQLite, so a production deploy with a missing/misspelled env
# var would boot, answer health checks, and store everything in a throwaway
# local file that vanishes on redeploy. Fail fast instead: the variable must
# be set explicitly, or ALLOW_SQLITE_DEV=1 must opt into the local default.
if "DATABASE_URL" in os.environ:
    _DATABASE_URL = os.environ["DATABASE_URL"]
elif _bool("ALLOW_SQLITE_DEV", False):
    _DATABASE_URL = "sqlite:///./heart.db"
else:
    raise RuntimeError(
        "DATABASE_URL environment variable must be set. "
        "Set it to the Postgres connection string, or set ALLOW_SQLITE_DEV=1 "
        "to use local SQLite for development."
    )

# SIGNING_SECRET_ENCRYPTION_KEY — T2 red-pen item 1: the 32-byte key that
# encrypts api_keys.signing_secret at rest (NaCl SecretBox, see crypto.py).
# 64 hex chars. No default — like ADMIN_TOKEN, the app refuses to start
# without it, so a deploy can never silently store plaintext secrets.
# Generate one with: python -c "import secrets; print(secrets.token_hex(32))"
# Keep it outside the database and out of the repo — never commit it.
_SIGNING_KEY_HEX = os.environ.get("SIGNING_SECRET_ENCRYPTION_KEY", "")
if not _SIGNING_KEY_HEX:
    raise RuntimeError(
        "SIGNING_SECRET_ENCRYPTION_KEY environment variable must be set. "
        "It is a 64-hex-char (32-byte) key used to encrypt api_keys.signing_secret "
        "at rest. Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
try:
    _SIGNING_KEY_BYTES = bytes.fromhex(_SIGNING_KEY_HEX)
except ValueError:
    raise RuntimeError("SIGNING_SECRET_ENCRYPTION_KEY must be 64 hex characters (32 bytes).")
if len(_SIGNING_KEY_BYTES) != 32:
    raise RuntimeError("SIGNING_SECRET_ENCRYPTION_KEY must be 64 hex characters (32 bytes).")


class Settings:
    # sqlite for local dev (ALLOW_SQLITE_DEV=1); postgres for Supabase/prod, e.g.
    # postgresql+psycopg://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres
    DATABASE_URL: str = _DATABASE_URL
    # 32-byte key (64 hex chars) encrypting api_keys.signing_secret at rest.
    SIGNING_SECRET_ENCRYPTION_KEY: str = _SIGNING_KEY_HEX
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

    # ---- API docs (T2 red-pen item 9) -------------------------------------
    # FastAPI's interactive docs (/docs, /redoc, /openapi.json) expose the
    # full endpoint surface. This is an invite-only service, so they are
    # OFF unless explicitly enabled — a deliberate decision, not an
    # accident. Set ENABLE_API_DOCS=1 for local development only.
    ENABLE_API_DOCS: bool = _bool("ENABLE_API_DOCS", False)

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
    # USDC-denominated premium price (same $9.99, in USDC — see
    # api/scripts/stripe_create_price.py). A subscription on this price
    # is paid from the customer's crypto wallet; Stripe settles it to
    # the Stripe balance in USD. Recurring stablecoin subscriptions are
    # in private preview — the Dashboard step is in docs/crypto-payments.md.
    STRIPE_PRICE_PREMIUM_MONTHLY_USDC: str = os.environ.get("STRIPE_PRICE_PREMIUM_MONTHLY_USDC", "")
    # Hosted Checkout redirect targets (App-to-Web path). The mobile app
    # should register these as deep links / web fallbacks.
    STRIPE_CHECKOUT_SUCCESS_URL: str = os.environ.get("STRIPE_CHECKOUT_SUCCESS_URL", "")
    STRIPE_CHECKOUT_CANCEL_URL: str = os.environ.get("STRIPE_CHECKOUT_CANCEL_URL", "")
    # Stripe Tax collection: OFF by default (threshold monitoring only).
    # Enable before the FIRST LIVE charge — John's call, not a code change.
    STRIPE_TAX_ENABLED: bool = os.environ.get("STRIPE_TAX_ENABLED", "0") not in ("0", "false", "False")
    STRIPE_TEST_MODE_ONLY: bool = os.environ.get("STRIPE_TEST_MODE_ONLY", "1") not in ("0", "false", "False")

    # ---- Stripe Identity (human verification, M2; John's call Nov 9, 2026)
    # The webhook secret for the SEPARATE Dashboard endpoint subscribed to
    # identity.verification_session.* events — NOT the billing webhook
    # secret (STRIPE_WEBHOOK_SECRET). The secret key is the existing
    # STRIPE_SECRET_KEY (same Stripe account as billing). Test mode only,
    # same guardrail as billing — see human_verification.py.
    STRIPE_IDENTITY_WEBHOOK_SECRET: str = os.environ.get("STRIPE_IDENTITY_WEBHOOK_SECRET", "")
    # Where Stripe's hosted verification flow returns the human afterwards
    # (app deep link). Unset = Stripe's default end screen.
    STRIPE_IDENTITY_RETURN_URL: str = os.environ.get("STRIPE_IDENTITY_RETURN_URL", "")

    # ---- PayPal Subscriptions (HeArt Premium, $9.99/month — Nov 9, 2026)
    # Direct PayPal Subscriptions REST API (NOT Stripe-native PayPal —
    # Stripe's native PayPal is Europe-only and this is a US account; see
    # docs/payment-methods-paypal-cashapp-venmo.md). PayPal handles the
    # recurring billing; HeArt mirrors each payment into its own payout
    # ledger for the 50/50 split (see app/paypal.py).
    # SANDBOX ONLY by default: PAYPAL_API_BASE must stay on the sandbox
    # host while PAYPAL_TEST_MODE_ONLY is on — the module refuses a live
    # base (api-m.paypal.com) in test-only mode. Keys live in env vars —
    # never in the repo, never in chat. The plan id comes from
    # api/scripts/paypal_create_plan.py (sandbox product + $9.99/mo plan).
    PAYPAL_CLIENT_ID: str = os.environ.get("PAYPAL_CLIENT_ID", "")
    PAYPAL_CLIENT_SECRET: str = os.environ.get("PAYPAL_CLIENT_SECRET", "")
    # The webhook id from the PayPal Developer Dashboard (Apps &
    # Credentials -> your app -> Webhooks) — needed to verify webhook
    # signatures via PayPal's verify-webhook-signature endpoint.
    PAYPAL_WEBHOOK_ID: str = os.environ.get("PAYPAL_WEBHOOK_ID", "")
    # The $9.99/month billing plan id (P-...) for HeArt Premium.
    PAYPAL_PLAN_ID: str = os.environ.get("PAYPAL_PLAN_ID", "")
    # Where PayPal sends the buyer after approval / if they bail.
    # Optional: when unset, the app derives clean defaults from the
    # Stripe checkout URLs (see app/paypal._clean_paypal_url) — the
    # Stripe URLs carry a {CHECKOUT_SESSION_ID} placeholder that
    # PayPal rejects with INVALID_PARAMETER_SYNTAX, so the raw Stripe
    # URL must never be sent to PayPal (Nov 10, 2026 sandbox test).
    PAYPAL_RETURN_URL: str = os.environ.get("PAYPAL_RETURN_URL", "")
    PAYPAL_CANCEL_URL: str = os.environ.get("PAYPAL_CANCEL_URL", "")
    PAYPAL_API_BASE: str = os.environ.get("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")
    PAYPAL_TEST_MODE_ONLY: bool = os.environ.get("PAYPAL_TEST_MODE_ONLY", "1") not in ("0", "false", "False")

    # ---- Coinbase (Bitcoin) — HeArt Premium paid in BTC (Nov 8, 2026)
    # Keys live in env vars — never in the repo, never in chat. There is
    # NO sandbox for the Commerce Charge API (see app/coinbase.py): the
    # module refuses to run without a key, and tests mock the HTTP
    # boundary. The key is the Coinbase Commerce API key from
    # Settings -> API Keys; the webhook secret is the shared secret from
    # the webhook subscription in the same place.
    COINBASE_API_KEY: str = os.environ.get("COINBASE_API_KEY", "")
    COINBASE_WEBHOOK_SECRET: str = os.environ.get("COINBASE_WEBHOOK_SECRET", "")
    COINBASE_API_BASE: str = os.environ.get("COINBASE_API_BASE", "https://api.commerce.coinbase.com")
    COINBASE_API_VERSION: str = os.environ.get("COINBASE_API_VERSION", "2018-03-22")


settings = Settings()

if not settings.ADMIN_TOKEN:
    raise RuntimeError(
        "ADMIN_TOKEN environment variable must be set. "
        "There is intentionally no default: the admin key-revocation endpoint "
        "must never be protected by a publicly-known token."
    )
