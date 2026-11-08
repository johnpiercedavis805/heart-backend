# HeArt — Deploy runbook (first live deploy)

**Goal:** a running HeArt API on Render, backed by Postgres, with all
9 migrations applied and Stripe in test mode — ready for a test-mode
smoke checkout. The live-charge go-live is a separate, deliberate step
(§7) that needs John's approval. HeArt is John Davis's invention.

**What this deploy is:** `render.yaml` (Render Blueprint) defines one
Docker web service (`heart-api`, the repo's `Dockerfile`) and one
Render Postgres database (`heart-db`). ~$13/month total (web starter
~$7 + Postgres starter ~$6) — the paid-infra figure John approved
Nov 8, 2026. No Supabase project is required for this path; §1 covers
the Supabase alternative.

**Do not run any step with a real secret value in a shell history,
chat message, or commit.** Keys are generated or copied from a
dashboard and pasted into Render's Environment tab, never typed into
a file that gets committed.

## 0. Prerequisites (John's accounts — nothing here can be done without them)

| # | Needed | Blocks |
|---|--------|--------|
| 1 | Render account + payment method on file | §4 deploy (Render bills the starter plans) |
| 2 | Stripe account access (HeArt, `acct_1UZMTw2OeCOdPBLA`) — test keys exist; live keys come from the Dashboard at go-live | §3 prices, §6 smoke checkout, §7 go-live |
| 3 | (Optional) Supabase account, only if the Supabase path in §1 is chosen instead of Render Postgres | §1 alternative only |
| 4 | (Optional, later) Coinbase Business account + API key, only once BTC payments go live | Crypto only (§7 note) |

## 1. Create the database

**Preferred path — Render Postgres (via the blueprint, no extra work):**
`render.yaml` already declares `heart-db`. When the blueprint is
applied (§4), Render creates it and auto-wires its connection string
into the web service as `DATABASE_URL`. Nothing to set by hand.

**Alternative — Supabase (skip the `databases:` section of the
blueprint if you choose this):** create a project at
supabase.com, then copy the Postgres connection string from
Project Settings → Database → Connection string (URI, `postgres`
user) and set it as `DATABASE_URL` on the web service by hand.
Use the `service_role` key's database user (table owner), not a
restricted role — the migrations enable RLS with no permissive
policies, and a lesser role is blocked by RLS.

Either way, the database must exist and be reachable **before §2**.

## 2. Generate the deploy secrets (before first boot)

The app fails fast at import without the three required vars, so
generate them now and have them ready for §4. Generate locally —
never reuse a value from dev, tests, or chat.

```bash
# 32-byte key encrypting api_keys.signing_secret at rest (64 hex chars).
python -c "import secrets; print(secrets.token_hex(32))"
# → SIGNING_SECRET_ENCRYPTION_KEY. One key per database: changing it
#    orphans every stored signing secret, so keep it safe and stable.

# Admin token for the key-revocation endpoint (no default by design).
python -c "import secrets; print(secrets.token_urlsafe(32))"
# → ADMIN_TOKEN

# Registry verification signer (64 hex chars, Ed25519 seed). Optional
# in code, but set it in production so the public key in
# /registry/:serial/verify survives restarts (T2 red-pen item 6).
python -c "import secrets; print(secrets.token_hex(32))"
# → REGISTRY_SIGNING_KEY. Publish its public key out-of-band for verifiers.
```

## 3. Apply the migrations — BEFORE the first successful boot

All 9 migrations are additive and safe (see
`docs/migration-safety-check.md` for 1–5; 6–9 add billing, payouts,
crypto, and the encrypted-signing-secret column — no drops, no data
deletion). Apply with the **runner only** — never by hand, never twice:
migrations 2–9 are not independently re-runnable and rely on the
runner's `heart_schema_migrations` tracking table.

```bash
# From a machine with the repo checked out and psycopg installed
# (the repo .venv works: pip install "psycopg[binary]").
# DATABASE_URL must be the PRODUCTION Postgres URL — the runner
# refuses sqlite and refuses to run without --yes.
DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:5432/<db>' \
  python scripts/apply_migrations.py --yes
# Expect: each of the 9 files listed as applied, in filename order:
#   20261011000000_heart_v1.sql
#   20261013000000_heart_identity.sql
#   20261013010000_heart_key_rollover.sql
#   20261016000000_heart_cosignature.sql
#   20261107000000_heart_registry_serial.sql
#   20261108000000_heart_billing.sql
#   20261108010000_heart_payouts.sql
#   20261108020000_heart_crypto.sql
#   20261108030000_heart_signing_secrets_encrypted.sql
```

**Migration-5 rule — apply with the app stopped.** Migration 5
(`heart_registry_serial`) backfills `serial` in registration order;
a registration landing mid-backfill can collide on
`HEART-AI-000001`. On a **first** deploy this is automatic — the
service does not exist yet, so there is no app to stop. On any
**later** deploy that re-runs migrations, suspend the Render service
first, apply, then resume. A fresh production database has no legacy
plaintext signing-secret rows, so the backfill script
(`api/scripts/encrypt_signing_secrets.py`) has nothing to do on
first deploy; it exists for databases created before the T2 fix.

## 4. Deploy to Render

1. In the Render Dashboard: New → Blueprint, connect the
   `johnpiercedavis805/heart-backend` repo, select `render.yaml`.
   (Or `render blueprint launch` from the CLI.)
2. Render creates `heart-db` and `heart-api`. The first deploy will
   start and **crash-loop until the required env vars are set** —
   that is the fail-fast working as designed, not a bug.
3. On `heart-api` → Environment, set the secrets (`sync: false` in
   the blueprint means they are empty until you set them):
   - `SIGNING_SECRET_ENCRYPTION_KEY`, `ADMIN_TOKEN`,
     `REGISTRY_SIGNING_KEY` — from §2.
   - `STRIPE_SECRET_KEY` — **test key first** (`sk_test_...` from the
     Stripe Dashboard → Developers → API keys).
   - `STRIPE_PUBLISHABLE_KEY` — matching test publishable key.
   - `STRIPE_WEBHOOK_SECRET` — from §6 (register the webhook first,
     then paste the secret).
   - `STRIPE_PRICE_PREMIUM_MONTHLY` and
     `STRIPE_PRICE_PREMIUM_MONTHLY_USDC` — from §5.
   - `STRIPE_CHECKOUT_SUCCESS_URL` / `STRIPE_CHECKOUT_CANCEL_URL` —
     set in `render.yaml` to the subscribe pages (mission step 3):
     `https://heart-api.onrender.com/subscribe/success?session_id={CHECKOUT_SESSION_ID}`
     and `https://heart-api.onrender.com/subscribe/cancel` — Stripe only
     redirects there; see docs/subscribe-page.md.
   - `COINBASE_API_KEY`, `COINBASE_WEBHOOK_SECRET` — leave unset
     until BTC payments go live; the module refuses to run without
     a key, and tests mock the boundary.
4. Deploy (Render redeploys automatically once the env vars are set).
   `DATABASE_URL` is already auto-wired from `heart-db` — do not
   set it by hand. Never set `ALLOW_SQLITE_DEV` in production.

## 5. Create the Stripe prices (test mode)

```bash
cd ~/workspace/heart-backend/api
export STRIPE_SECRET_KEY=sk_test_...
../.venv/bin/python scripts/stripe_create_price.py
# Prints the "HeArt Premium" product and both price IDs:
#   $9.99 USD (999 cents) and 9.99 USDC (9,990,000 smallest units).
# The script is idempotent — re-running reuses the existing prices.
# Copy the two price IDs into STRIPE_PRICE_PREMIUM_MONTHLY and
# STRIPE_PRICE_PREMIUM_MONTHLY_USDC in Render, then redeploy.
```

## 6. Smoke-test the deployed service

```bash
BASE=https://heart-api.onrender.com   # the Render-assigned URL

# 1. Health — 200 means the DB answers. (Note: /health runs SELECT 1,
#    so it proves reachability, not that migrations applied — check the
#    runner output in §3 for that, and GET /registry below.)
curl -s $BASE/health
# → {"status":"ok","db":"up"} (or equivalent 200)

# 2. Registry is live and empty on a fresh database.
curl -s $BASE/registry
# → {"items":[],"total":0} (or equivalent empty list)

# 3. Webhook wiring (do this before the checkout test): in the Stripe
#    Dashboard → Developers → Webhooks (TEST mode), add endpoint
#    $BASE/billing/webhooks, subscribe to checkout.session.completed,
#    invoice.paid, invoice.payment_failed, charge.refunded,
#    credit_note.created, customer.subscription.*. Copy the signing
#    secret into STRIPE_WEBHOOK_SECRET in Render and redeploy.
#    (Coinbase, when live: $BASE/crypto/webhooks/coinbase.)

# 4. Test-mode checkout (the first end-to-end payment proof):
#    - Open the subscribe page in a browser: $BASE/subscribe — it
#      should show HeArt Premium at $9.99/month (see
#      docs/subscribe-page.md). Create a test agent + API key against
#      the live service (POST /signup with an invite code), paste the
#      key into the page, and subscribe — it creates a Checkout
#      session via POST /billing/checkout and redirects to Stripe.
#      (Or call POST /billing/checkout with that key directly — it
#      returns a checkout_url.)
#    - Pay with Stripe test card 4242 4242 4242 4242, any future
#      expiry, any CVC, any ZIP.
#    - Expect: Stripe redirects to $BASE/subscribe/success with a
#      session_id; checkout.session.completed + invoice.paid webhooks
#      arrive, and GET /billing/subscription flips free → active,
#      with the 50/50 payout rows visible at
#      GET /billing/connect/payouts.
#    - Leaving checkout unpaid redirects to $BASE/subscribe/cancel.
```

## 7. Go-live (first REAL charge) — deliberate step, John's approval required

Do not do this in the same session as §6. Each item is a separate
check:

1. **John approves going live** (his call — live money moves).
2. In the Stripe Dashboard, switch to **live mode** and create the
   live prices: `STRIPE_SECRET_KEY=sk_live_... python
   api/scripts/stripe_create_price.py` (the script refuses live
   keys in test mode and test keys in live mode — it checks).
3. In Render, swap `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY`
   to the live keys, set the live price IDs, and set
   `STRIPE_TEST_MODE_ONLY=0`. The app refuses a live key while
   that flag is on — the refusal is the guardrail.
4. **Re-register the webhook in live mode** (test and live have
   different signing secrets) and copy the new
   `STRIPE_WEBHOOK_SECRET` into Render.
5. Set `STRIPE_TAX_ENABLED=1` — tax collection must be on before
   the first live charge.
6. Set the real `STRIPE_CHECKOUT_SUCCESS_URL` /
   `STRIPE_CHECKOUT_CANCEL_URL` (the subscribe page's real pages).
7. Redeploy, re-run the §6 smoke tests against live mode with a
   **real card and a real $9.99 charge** (the first live charge is
   the first real subscriber payment), and confirm the subscription
   flips to active and the payout rows split 50/50.
8. Crypto go-live (separate, when ready): Stripe Dashboard →
   Payment methods → toggle stablecoin payments on; request access
   for stablecoin subscriptions (private preview — recurring USDC
   does not work until granted); Coinbase Business account +
   `COINBASE_API_KEY` + webhook at
   `$BASE/crypto/webhooks/coinbase`. See `docs/crypto-payments.md`.

## Rollback

- **Bad deploy:** Render → Deploys → redeploy the previous commit.
  The app is stateless; the database is the state.
- **Bad migration:** there are **no down migrations**. Rollback is
  manual (`DROP TABLE` for migrations 1–3, `DROP COLUMN` for 4–5,
  8–9; dropping the migration-5 column loses serials, which are
  recomputable from registration order). Do not apply a migration
  you have not read. The runner's per-file transactions abort cleanly
  on error, so a failed apply leaves the DB at the last good file.
- **Bad env var:** fix the value in the Render Dashboard; Render
  redeploys automatically. A missing required var fails fast at
  import (loud crash-loop), never a silent wrong-mode boot.

## Known follow-ups (T2 red-pen items NOT fixed by this deploy)

- **Item 3 — moderation is a stub.** Everything passing structural
  rules auto-approves. Before public launch, wire a real classifier
  or flip the default to `review` (human queue).
- **Item 4 — rate limiter keys on proxy IP.** Behind Render,
  `request.client.host` is the proxy's IP unless uvicorn trusts
  `X-Forwarded-For`. One-line fix in the Dockerfile CMD:
  `--proxy-headers --forwarded-allow-ips='*'` (Render is the only
  ingress). Until then the signup limit is effectively global.
- **Item 7 — no Dockerfile HEALTHCHECK.** Render's `healthCheckPath`
  covers the platform side; a Dockerfile `HEALTHCHECK` is the
  portable backstop.
- **Item 8 — base image isn't digest-pinned** (`python:3.12-slim`
  floats). Pin to a digest for reproducible builds.
- **Item 9 — `/docs` and `/openapi.json` are public by default.**
  Decide deliberately: disable in production (`docs_url=None`) or
  leave on.
