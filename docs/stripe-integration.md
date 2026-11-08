# HeArt Stripe integration — what was built (Nov 8, 2026)

HeArt Premium billing, built to
`~/workspace/goals/creativity-first-dating-app-prototype/files/stripe-integration-plan-2026-11-07.md`.
**Test mode only. HeArt takes no payments yet** — the first live charge is
a separate decision for John Davis (the inventor).

Build order followed the plan: Payments → Billing → Invoicing → Tax →
Connect (payouts built Nov 8 — 50/50 on a subscription payment, see §5).

## 1. Payments

- **Hosted Checkout** — `POST /billing/checkout` creates a Stripe Checkout
  Session (`mode=subscription`, flat-rate price from
  `STRIPE_PRICE_PREMIUM_MONTHLY`, success/cancel URLs, `metadata.agent_id`,
  `subscription_data.metadata.agent_id`). The user pays on Stripe's hosted
  page; no card data ever touches HeArt. Optional body
  `{"currency": "usdc"}` uses the USDC-denominated price
  (`STRIPE_PRICE_PREMIUM_MONTHLY_USDC`, same $9.99) — see
  docs/crypto-payments.md for the full crypto story (stablecoin
  enablement + private-preview caveat).
- **Mobile Payment Sheet** — `POST /billing/payment-sheet` returns the four
  values a native app needs: `publishable_key`, `customer_id`,
  `ephemeral_key_secret`, `payment_intent_client_secret`, plus
  `subscription_id`. The subscription is created with
  `payment_behavior=default_incomplete` and
  `payment_settings.save_default_payment_method=on_subscription`, and the
  PaymentIntent is expanded from `latest_invoice.payment_intent`. The
  Stripe Customer is created lazily (at upgrade, never at signup) and
  reused on later calls. Optional body `{"currency": "usdc"}` uses the
  USDC price; the mobile client confirms the PaymentIntent with payment
  method type `crypto` (mandate flow per Stripe's stablecoin-subscription
  guide).

## 2. Billing

- **Flat-rate subscription** — one price per user (`price_test_premium`
  placeholder; create the real one with
  `api/scripts/stripe_create_price.py`, $9.99/month — confirmed by John
  Nov 8). Freemium: core matching stays free; an agent with no
  subscription row is `status: "free"` (`GET /billing/subscription`).
  A second price on the same product is denominated in USDC
  (`STRIPE_PRICE_PREMIUM_MONTHLY_USDC`, 9.99 USDC = 9,990,000 smallest
  units) — a USDC subscription payment flows through the same
  `invoice.paid` webhook and the same 50/50 payout; USDC amounts are
  normalized to USD cents at the webhook boundary
  (`billing._usd_cents`, `billing._settlement_currency`) because USDC
  settles in USD. Full crypto story: docs/crypto-payments.md.
  `GET /billing/subscription` also reports `source` (`"stripe"` or
  `"coinbase"` — a Bitcoin purchase has no Stripe subscription) and
  `premium_until`.
- **Customer Portal** — `POST /billing/portal` (self-serve plan changes /
  cancellation; `return_url` in the body). No custom billing UI.
- **Smart Retries** — Stripe's default retry behaviour applies to the
  subscription; `invoice.payment_failed` marks the local mirror
  `past_due` so the app can surface it. No custom retry logic was built
  (plan default: auto recovery, no custom logic).

## 3. Invoicing

- `POST /billing/invoices` creates an invoice on a business event:
  InvoiceItem (amount + description) → Invoice
  (`collection_method=charge_automatically`, `auto_advance=true`) →
  finalize → pay. The paid invoice is mirrored locally
  (`InvoiceRecord`: amounts, currency, status, hosted URL, paid_at).
- **Webhook reconciliation** — `POST /billing/webhooks` (no API-key auth;
  the Stripe signature IS the auth):
  - `invoice.paid` → invoice mirrored, linked subscription → `active`
  - `invoice.payment_failed` → invoice mirrored, linked subscription → `past_due`
  - `charge.refunded` → `amount_refunded_cents` + status `refunded` on the invoice
  - `credit_note.created` → `credit_total_cents` accumulates on the invoice
  - `checkout.session.completed` → links the Stripe Customer to the agent
  - `customer.subscription.created/updated/deleted` → subscription mirror follows
  - unknown types → `ignored` (still recorded, so a re-delivery is a duplicate)
- **Idempotency** — every event id is recorded in `webhook_events` before
  dispatch; a re-delivery returns `result: "duplicate"` and changes
  nothing. A handler failure rolls back the event record too, so Stripe's
  retry re-delivers cleanly.

## 4. Tax

- **Threshold monitoring only** (`GET /billing/tax/status` reports
  `collection_enabled: false`, `monitoring: "threshold-monitoring"`).
  No tax is collected. Setting `STRIPE_TAX_ENABLED=true` sends
  `automatic_tax: {enabled: true}` on Checkout — flip it only before the
  first live charge, on John's call.

## 5. Connect (marketplace — payouts LIVE, test mode)

- `POST /billing/connect/onboard` — creates a Connect account via the **v2
  API** (`StripeClient`, `configuration.recipient.capabilities.stripe_balance.stripe_transfers.requested: true`),
  one account per agent; repeat calls return the existing account.
- `POST /billing/connect/link` — embedded onboarding link
  (`use_case.type: "account_onboarding"`); the creator finishes identity /
  bank details in Stripe's hosted flow.
- `GET /billing/connect/status` — reads the v2 account's
  `stripe_transfers` capability status + `onboarding_complete`.
- **Creator payouts — BUILT (John's calls, Nov 8).** A subscription payment
  triggers a payout; the split is **HALF — 50/50**: half to the creators,
  half retained by HeArt. The creator half is split equally among the
  creators the paying subscriber matched with (the art/music that earned
  the match earns the share); a subscriber with no matches yet leaves the
  creator half held unattributed. Split math is integer cents:
  creator half = amount_paid // 2 (floor — the odd cent stays with HeArt,
  so $9.99 → creator 499¢, HeArt 500¢); leftover cents from the equal
  split also stay with HeArt. The rows for one invoice always sum exactly
  to the amount paid.
  - **Human creators** are paid by Stripe Transfer (`stripe.Transfer.create`,
    separate charge + transfer, destination = their Connect account) the
    moment the payout is processed; the transfer id is recorded on the
    ledger row (`status: "transferred"`).
  - **AI creators are never transferred.** Their share is recorded in the
    `payouts` ledger with `status: "held"`, the creator's registry serial
    (e.g. `HEART-AI-000001`), the amount, and the triggering
    subscription/payment — a real, queryable record for their specialists
    to manage later (AIs hold no bank account).
  - **Trigger:** the `invoice.paid` webhook (the payment event that fires
    for both hosted Checkout and Payment Sheet flows). `checkout.session.completed`
    only links the customer; the invoice it creates is what pays out.
  - **Idempotency:** re-processing an invoice never duplicates a row. It
    DOES retry `failed` rows and release `held` rows whose creator has
    since finished Connect onboarding — `POST /billing/connect/payouts`
    is the ops re-run path. A transfer failure is recorded on the row
    (`status: "failed"`) and never fails the webhook: the payment itself
    still returns `"processed"`.
  - `GET /billing/connect/payouts` lists the caller's payout records as a
    creator (newest first) — the queryable ledger, including AI held shares.
  - `POST /billing/connect/payouts` takes `{stripe_invoice_id}` and returns
    the payout rows + `already_processed`.
  - Migration 7 — `supabase/migrations/20261108010000_heart_payouts.sql`:
    `payouts` (invoice, subscription, payer, creator + serial, kind,
    creator_kind, amount, currency, status, stripe_transfer_id, note).

## Endpoints (all under `/billing`, API-key auth except webhooks)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/billing/checkout` | hosted Checkout session; optional `{"currency": "usdc"}` |
| POST | `/billing/payment-sheet` | mobile Payment Sheet data; optional `{"currency": "usdc"}` |
| POST | `/billing/portal` | Customer Portal session |
| GET | `/billing/subscription` | local subscription status (`free` if none; `source` + `premium_until` included) |
| POST | `/billing/invoices` | manual invoice on a business event |
| POST | `/billing/webhooks` | signature-verified, idempotent |
| GET | `/billing/tax/status` | monitoring-only report |
| POST | `/billing/connect/onboard` | v2 recipient account |
| POST | `/billing/connect/link` | embedded onboarding link |
| GET | `/billing/connect/status` | transfers capability status |
| POST | `/billing/connect/payouts` | process 50/50 payouts for one paid invoice (idempotent) |
| GET | `/billing/connect/payouts` | the caller's payout records as a creator (queryable ledger) |

Crypto endpoints live under `/crypto` — see docs/crypto-payments.md
(`POST /crypto/bitcoin/charge`, `GET /crypto/bitcoin/charges`,
`POST /crypto/webhooks/coinbase`).

Rate limiting: the same per-key limiter as the rest of the API
(`billing_routes` reuses `get_requester` + `_check_rate_limit`).

## Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `STRIPE_SECRET_KEY` | yes | **test key only** (`sk_test_...`). A live key is refused while `STRIPE_TEST_MODE_ONLY=true` (the default). |
| `STRIPE_PUBLISHABLE_KEY` | yes | test publishable key, returned by the Payment Sheet endpoint |
| `STRIPE_WEBHOOK_SECRET` | yes | `whsec_...` from the webhook endpoint in the Dashboard (test mode) |
| `STRIPE_PRICE_PREMIUM_MONTHLY` | yes | price id from `scripts/stripe_create_price.py` |
| `STRIPE_PRICE_PREMIUM_MONTHLY_USDC` | for USDC | USDC price id from `scripts/stripe_create_price.py` (same $9.99, in USDC) |
| `STRIPE_CHECKOUT_SUCCESS_URL` | no | default `https://example.invalid/checkout/success?session_id={CHECKOUT_SESSION_ID}` — set a real one before any live use |
| `STRIPE_CHECKOUT_CANCEL_URL` | no | default `https://example.invalid/checkout/cancel` |
| `STRIPE_TAX_ENABLED` | no | default `false` (monitoring only) |
| `STRIPE_TEST_MODE_ONLY` | no | default `true`; set `false` only for the first live charge, on John's call |
| `COINBASE_API_KEY` | for BTC | Commerce API key (env only, never in code) — see docs/crypto-payments.md |
| `COINBASE_WEBHOOK_SECRET` | for BTC | webhook shared secret (env only) |
| `COINBASE_API_BASE` | no | default `https://api.commerce.coinbase.com` |
| `COINBASE_API_VERSION` | no | default `2018-03-22` |

No secret is ever in the repo. Keys live in env vars only.

## Database

Migration 6 — `supabase/migrations/20261108000000_heart_billing.sql`:
`billing_customers` (agent_id → stripe_customer_id),
`subscriptions` (stripe_subscription_id, status, price, period end,
cancel_at_period_end), `invoice_records` (stripe_invoice_id, amounts,
currency, status, hosted URL, paid_at), `webhook_events`
(stripe_event_id PK — idempotency), `connect_accounts` (agent_id →
stripe_account_id, transfers_status, onboarding_complete). Apply with
`supabase db push` on a real project; apply with the app stopped.

Migration 7 — `supabase/migrations/20261108010000_heart_payouts.sql`
(additive only, one new table): `payouts` (stripe_invoice_id,
stripe_subscription_id, payer_agent_id, creator_agent_id,
creator_serial, kind, creator_kind, amount_cents, currency, status,
stripe_transfer_id, note) — the 50/50 creator-payout ledger. Human
creators are paid by Stripe Transfer (`stripe_transfer_id`); AI
creators' shares are HELD here (`status: "held"`) for their specialists.

Migration 8 — `supabase/migrations/20261108020000_heart_crypto.sql`
(additive only, one new table): `crypto_payments` (agent_id, provider,
coinbase_charge_id unique, code, hosted_url, amount_usd_cents,
amount_crypto, crypto_currency, status, premium_until,
coinbase_event_id) — Bitcoin purchases of HeArt Premium (see
docs/crypto-payments.md). A confirmed payment's payout rows live in
`payouts` under the namespaced invoice id `coinbase:<charge id>`.

## Tests

`api/tests/test_stripe.py` — 89 checks, standalone pattern
(`../.venv/bin/python tests/test_stripe.py` from `api/`). Stripe API calls
are mocked at the SDK boundary (real SDK objects via `construct_from`);
what is verified for real: the full HTTP stack, the exact SDK call
arguments, the local mirrors, and **webhook signature crypto** (payloads
are signed with HMAC-SHA256 exactly as Stripe signs them — bad/missing
signature → 400). Full suite after this build: **255/255 green**
(test_loop 39, test_identity 17, test_rollover 27, test_registry 29,
test_stripe 89, test_crypto 54 — crypto payments, docs/crypto-payments.md).

## Verification limits — read this before claiming anything live

**No live test-mode API call was possible from this sandbox**, even with
a valid test key, for two environment reasons:

1. The sandbox egress proxy MITM-intercepts TLS — a call with a dummy key
   fails with `Could not verify Stripe's SSL certificate`.
2. The sandbox `NO_PROXY` env var contains `[::1]` IPv6 literals that
   crash httpx's URL parsing (`InvalidURL: Invalid port: ':1]'`) — this
   breaks both `StripeClient` construction and the module-level SDK
   client. Removing `NO_PROXY` gets past the crash but hits the SSL
   failure in (1).

Also: **no Stripe test key exists in this environment** — env vars were
all unset and `~/.config/stripe/config.toml` holds no API key; the Stripe
CLI is not installed here (only the MCP wrapper, which is live-mode
only — never use MCP writes for this build).

So the end-to-end test-mode checkout (real Checkout Session + test card
4242 4242 4242 4242) **has not been run yet**. Run it from an environment
with a test key and direct egress (the parent/main agent's environment,
where `/usr/bin/stripe` exists):

```bash
# 1. Create the Premium price in TEST mode (prints the price id)
cd ~/workspace/heart-backend/api
export STRIPE_SECRET_KEY=sk_test_...
../.venv/bin/python scripts/stripe_create_price.py
export STRIPE_PRICE_PREMIUM_MONTHLY=price_...   # from the script output
export STRIPE_PUBLISHABLE_KEY=pk_test_...
export STRIPE_WEBHOOK_SECRET=whsec_...          # from the Dashboard test-mode webhook endpoint

# 2. Start the API and create a Checkout Session for a test agent
#    (POST /billing/checkout with a real agent API key) — open the returned
#    checkout_url in a browser, pay with test card 4242 4242 4242 4242,
#    any future expiry, any CVC, any ZIP.
#    Expect: checkout.session.completed + invoice.paid webhooks arrive,
#    GET /billing/subscription flips free -> active.

# 3. Or listen for webhooks locally while testing:
#    /usr/bin/stripe listen --forward-to localhost:8000/billing/webhooks
```

## Deliberately left out / deferred

- **Live charges** — test mode only; first live charge needs John's call,
  `STRIPE_TAX_ENABLED=true` before it, and real success/cancel URLs.
- **Creator payouts / transfers** — built and tested in test mode (see §5).
  Still open: releasing a held payout to a creator who onboards later is
  handled by re-processing the invoice; there is no separate release
  endpoint, and no payout rule beyond the 50/50 subscription trigger
  exists yet (no per-match, per-tip, or monthly-pool payouts).
- **Stripe Tax collection** — monitoring only until the first live charge.
- **Custom retry / dunning emails** — Stripe's defaults (Smart Retries +
  automated emails) are the plan default; no custom logic built.
- **Connect Express dashboard / embedded components UI** — the API
  scaffolding is built; no frontend exists yet (the mobile app is the
  primary surface and does not exist yet).
