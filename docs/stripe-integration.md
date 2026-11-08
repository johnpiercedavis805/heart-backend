# HeArt Stripe integration — what was built (Nov 8, 2026)

HeArt Premium billing, built to
`~/workspace/goals/creativity-first-dating-app-prototype/files/stripe-integration-plan-2026-11-07.md`.
**Test mode only. HeArt takes no payments yet** — the first live charge is
a separate decision for John Davis (the inventor).

Build order followed the plan: Payments → Billing → Invoicing → Tax →
Connect (scaffolding only; payouts gated).

## 1. Payments

- **Hosted Checkout** — `POST /billing/checkout` creates a Stripe Checkout
  Session (`mode=subscription`, flat-rate price from
  `STRIPE_PRICE_PREMIUM_MONTHLY`, success/cancel URLs, `metadata.agent_id`,
  `subscription_data.metadata.agent_id`). The user pays on Stripe's hosted
  page; no card data ever touches HeArt.
- **Mobile Payment Sheet** — `POST /billing/payment-sheet` returns the four
  values a native app needs: `publishable_key`, `customer_id`,
  `ephemeral_key_secret`, `payment_intent_client_secret`, plus
  `subscription_id`. The subscription is created with
  `payment_behavior=default_incomplete` and
  `payment_settings.save_default_payment_method=on_subscription`, and the
  PaymentIntent is expanded from `latest_invoice.payment_intent`. The
  Stripe Customer is created lazily (at upgrade, never at signup) and
  reused on later calls.

## 2. Billing

- **Flat-rate subscription** — one price per user (`price_test_premium`
  placeholder; create the real one with
  `api/scripts/stripe_create_price.py`, default $9.99/month — John's call
  to change). Freemium: core matching stays free; an agent with no
  subscription row is `status: "free"` (`GET /billing/subscription`).
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

## 5. Connect (marketplace scaffolding — payouts NOT live)

- `POST /billing/connect/onboard` — creates a Connect account via the **v2
  API** (`StripeClient`, `configuration.recipient.capabilities.stripe_balance.stripe_transfers.requested: true`),
  one account per agent; repeat calls return the existing account.
- `POST /billing/connect/link` — embedded onboarding link
  (`use_case.type: "account_onboarding"`); the creator finishes identity /
  bank details in Stripe's hosted flow.
- `GET /billing/connect/status` — reads the v2 account's
  `stripe_transfers` capability status + `onboarding_complete`.
- **Creator payouts are GATED, not built.** `POST /billing/connect/payouts`
  returns **409** with the message that payouts are not enabled pending
  John's decision on the payout trigger (and how an AI participant's share
  is held, since AIs hold no bank account). `create_creator_transfer()`
  raises `PayoutsNotEnabled` and never creates a transfer. Do not wire a
  transfer call until John answers that question.

## Endpoints (all under `/billing`, API-key auth except webhooks)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/billing/checkout` | hosted Checkout session |
| POST | `/billing/payment-sheet` | mobile Payment Sheet data |
| POST | `/billing/portal` | Customer Portal session |
| GET | `/billing/subscription` | local subscription status (`free` if none) |
| POST | `/billing/invoices` | manual invoice on a business event |
| POST | `/billing/webhooks` | signature-verified, idempotent |
| GET | `/billing/tax/status` | monitoring-only report |
| POST | `/billing/connect/onboard` | v2 recipient account |
| POST | `/billing/connect/link` | embedded onboarding link |
| GET | `/billing/connect/status` | transfers capability status |
| POST | `/billing/connect/payouts` | 409 gate — payouts not enabled |

Rate limiting: the same per-key limiter as the rest of the API
(`billing_routes` reuses `get_requester` + `_check_rate_limit`).

## Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `STRIPE_SECRET_KEY` | yes | **test key only** (`sk_test_...`). A live key is refused while `STRIPE_TEST_MODE_ONLY=true` (the default). |
| `STRIPE_PUBLISHABLE_KEY` | yes | test publishable key, returned by the Payment Sheet endpoint |
| `STRIPE_WEBHOOK_SECRET` | yes | `whsec_...` from the webhook endpoint in the Dashboard (test mode) |
| `STRIPE_PRICE_PREMIUM_MONTHLY` | yes | price id from `scripts/stripe_create_price.py` |
| `STRIPE_CHECKOUT_SUCCESS_URL` | no | default `https://example.invalid/checkout/success?session_id={CHECKOUT_SESSION_ID}` — set a real one before any live use |
| `STRIPE_CHECKOUT_CANCEL_URL` | no | default `https://example.invalid/checkout/cancel` |
| `STRIPE_TAX_ENABLED` | no | default `false` (monitoring only) |
| `STRIPE_TEST_MODE_ONLY` | no | default `true`; set `false` only for the first live charge, on John's call |

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

## Tests

`api/tests/test_stripe.py` — 61 checks, standalone pattern
(`../.venv/bin/python tests/test_stripe.py` from `api/`). Stripe API calls
are mocked at the SDK boundary (real SDK objects via `construct_from`);
what is verified for real: the full HTTP stack, the exact SDK call
arguments, the local mirrors, and **webhook signature crypto** (payloads
are signed with HMAC-SHA256 exactly as Stripe signs them — bad/missing
signature → 400). Full suite after this build: **173/173 green**
(test_loop 39, test_identity 17, test_rollover 27, test_registry 29,
test_stripe 61).

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
- **Creator payouts / transfers** — gated (409); needs John's payout-trigger
  answer + the AI-share holding question.
- **Stripe Tax collection** — monitoring only until the first live charge.
- **Custom retry / dunning emails** — Stripe's defaults (Smart Retries +
  automated emails) are the plan default; no custom logic built.
- **Connect Express dashboard / embedded components UI** — the API
  scaffolding is built; no frontend exists yet (the mobile app is the
  primary surface and does not exist yet).
- **Price amount** — $9.99/month is the plan default in the helper script;
  John has not confirmed a price.
