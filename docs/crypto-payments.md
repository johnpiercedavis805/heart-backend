# HeArt crypto payments — USDC (Stripe) + Bitcoin (Coinbase)

John's calls, Nov 8: users can pay for HeArt Premium with crypto — BOTH
USDC (via Stripe) and Bitcoin (via Coinbase Business). Price is the same
either way: **$9.99**. The 50/50 creator-payout split applies to crypto
payments exactly as it does to card payments, computed on the USD value.

**Test mode / sandbox only. HeArt takes no payments yet** — the first
live charge is a separate decision for John Pierce Davis Jr. (the inventor and CEO).

## Part 1 — USDC via Stripe (stablecoin)

USDC is a stablecoin pegged to the dollar, so $9.99 stays $9.99 — no
Bitcoin-style price swings. The user pays from a wallet (MetaMask,
Phantom, Coinbase Wallet, …) and Stripe settles the payment into HeArt's
Stripe balance in USD, so the payout math is unchanged.

- **Price** — a second price on the same HeArt Premium product,
  denominated in USDC: `STRIPE_PRICE_PREMIUM_MONTHLY_USDC`. Created by
  `api/scripts/stripe_create_price.py` alongside the USD price (9.99
  USDC = 9,990,000 smallest units — USDC has 6 decimals in Stripe's
  API). Same $9.99, flat rate, monthly.
- **Checkout / Payment Sheet** — `POST /billing/checkout` and
  `POST /billing/payment-sheet` take an optional `currency` field
  (`"usd"` default, `"usdc"`). With `"usdc"` the USDC price is used;
  the crypto option appears in Checkout / the Payment Sheet once
  stablecoins are enabled in the Dashboard (dynamic payment methods).
  The customer picks a token and network on `crypto.stripe.com` and
  connects a wallet. For the Payment Sheet, the mobile client confirms
  the PaymentIntent with payment method type `crypto` (mandate flow —
  the wallet is saved once and authorizes recurring debits, per
  Stripe's stablecoin-subscription guide:
  https://docs.stripe.com/billing/subscriptions/stablecoins).
- **Same payout path** — a USDC subscription payment fires the same
  `invoice.paid` webhook and the same 50/50 payout as a card payment
  (`billing.process_payouts_for_invoice`). USDC amounts arrive from
  Stripe in micro-USDC; `billing._usd_cents()` normalizes them to USD
  cents at the webhook boundary (9,990,000 → 999) and the local
  mirrors record the settlement currency (`usd`). The payout ledger
  and the Stripe Transfers downstream are all USD.
- **Fee** — stablecoin payments cost **1.5%** of the transaction in
  USD (conversion, AML screening, gas included), vs 2.9% + $0.30 for a
  US card. Per-transaction cap $10,000. No chargebacks/disputes on
  stablecoin payments (the customer authenticates the payment).
- **Accepted tokens** — USDC on Tempo, Ethereum, Solana, Polygon,
  Base; USDP (Ethereum, Solana) and USDG (Ethereum), US-only.

### Dashboard steps John must do (Stripe)

1. **Enable stablecoins**: Dashboard → Settings → Payment methods →
   scroll to the cryptocurrencies section → toggle stablecoin payments
   on. (US businesses can switch this on; the EU, Hong Kong, Mexico
   and Switzerland are in private preview.)
2. **Request access for stablecoin subscriptions**: recurring USDC
   billing is in **private preview** — the one-time payment method
   above does not by itself enable recurring subscriptions. Request
   access in the Dashboard (the stablecoin-subscription preview) and
   confirm it is granted before claiming recurring USDC works.
   Until then the code is ready but the recurring flow is not live.
3. **Create the prices** (test mode):
   ```bash
   cd ~/workspace/heart-backend/api
   export STRIPE_SECRET_KEY=sk_test_...
   ../.venv/bin/python scripts/stripe_create_price.py
   # prints STRIPE_PRICE_PREMIUM_MONTHLY and STRIPE_PRICE_PREMIUM_MONTHLY_USDC
   ```
4. **Test it** (testnet assets, no cost): Stripe's guide recommends
   MetaMask + Polygon Amoy testnet + the Circle faucet (20 test USDC).
   Open the payment redirect page with test API keys, connect the
   wallet, complete the payment, and confirm the redirect + the
   `invoice.paid` webhook + the 50/50 payout rows.

## Part 2 — Bitcoin via Coinbase

### The honest state of Coinbase's APIs (checked Nov 8, 2026)

- Coinbase Business's **current** API — the Checkouts API on the
  Coinbase Developer Platform — accepts **USDC (and USDT on
  Ethereum) ONLY. It cannot take Bitcoin at all.**
- The **only** Coinbase API that accepts Bitcoin is the legacy
  Commerce **Charge API** (`POST https://api.commerce.coinbase.com/charges`).
  Coinbase still operates it for US merchants — its own FAQ (Nov 4,
  2026) says "Existing Commerce webhooks will continue to work for
  the Charge API" — but recommends migrating to the Checkouts API,
  and its Node library is no longer actively maintained. It may be
  retired later.
- So the Bitcoin flow uses the Charge API, deliberately, because it
  is the only Coinbase route to Bitcoin. The integration is isolated
  behind one small module (`api/app/coinbase.py`: one HTTP function,
  one webhook handler) so swapping the Bitcoin rail later — BitPay,
  BTCPay Server — is a contained change: replace `_coinbase_request`
  + the webhook event names, keep the `crypto_payments` ledger and the
  payout trigger exactly as they are.

### How it works

- **One-time $9.99 charge** — `POST /crypto/bitcoin/charge` creates a
  fixed-price charge priced in USD (`local_price: {amount: "9.99",
  currency: "USD"}`) and returns Coinbase's hosted checkout URL. The
  payer picks the coin at checkout (Bitcoin is the expected coin, but
  the checkout offers every coin Coinbase supports); the CONFIRMED
  webhook's payment value is what was actually paid, recorded on the
  `crypto_payments` row (`amount_crypto`, `crypto_currency`).
- **Confirmation** — `POST /crypto/webhooks/coinbase` receives
  Coinbase's webhooks. The signature is verified first (HMAC-SHA256
  over the raw body, `X-CC-Webhook-Signature` header, webhook shared
  secret — bad/missing signature → 400, nothing processed). Only
  `charge:confirmed` activates premium and triggers the payout — and
  only once (a re-delivery for an already confirmed charge is a
  duplicate). `charge:failed` marks the charge failed (no premium, no
  payout); `charge:pending` records the on-chain sighting and nothing
  else. Never fulfill from the redirect alone.
- **Premium** — a confirmed payment activates premium for **30 days**
  (`premium_until`). `GET /billing/subscription` reports it as
  `status: "active"` with `source: "coinbase"`. There is no Stripe
  subscription for a BTC purchase.
- **Payout** — the same 50/50 split as a Stripe subscription payment,
  computed on the USD value ($9.99 → creator 499¢, HeArt 500¢), via a
  synthetic invoice record namespaced `coinbase:<charge id>` (there is
  no Stripe invoice for a Coinbase payment; the payout ledger keys on
  that reference). Human creators are paid by Stripe Transfer; AI
  creators' shares are HELD in the ledger, exactly as for card/USDC
  payments. `GET /billing/connect/payouts` lists them the same way.
- **No auto-renew** — Bitcoin cannot auto-renew. There is no
  card-on-file equivalent, so each month is a fresh charge the user
  pays by hand. Charges expire after **1 hour** if unpaid (Coinbase's
  rule) — the user just creates a fresh one.
- **Fee** — Coinbase Commerce charges **~1% flat**, no monthly fee
  (its published rate — confirm in the account).

### Steps John must do (Coinbase)

1. **Open a Coinbase Business account** (US — the product is open to
   US and Singapore businesses) and complete merchant verification.
2. **API key**: in the Commerce dashboard, Settings → API Keys →
   Create an API Key. Copy it immediately (shown only once) and set
   it as `COINBASE_API_KEY` in the deployment's env vars. Never paste
   it in chat or code.
3. **Webhook secret + subscription**: Settings → Webhook
   subscriptions → add the endpoint URL
   `https://<your-api-host>/crypto/webhooks/coinbase` → copy the
   webhook shared secret and set it as `COINBASE_WEBHOOK_SECRET`.
4. **Test it**: create a charge for $9.99, pay it on the hosted
   checkout with a small real BTC payment (there is **no sandbox**
   for the Charge API — Coinbase never shipped one), and confirm the
   webhook arrives, premium activates for 30 days, and the 50/50
   payout rows appear.

## Endpoints (new)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/billing/checkout` | now takes optional `{"currency": "usdc"}` (default `"usd"`) |
| POST | `/billing/payment-sheet` | now takes optional `{"currency": "usdc"}` (default `"usd"`) |
| POST | `/crypto/bitcoin/charge` | one-time $9.99 BTC charge → hosted checkout URL (201) |
| GET | `/crypto/bitcoin/charges` | the caller's BTC charges + premium windows |
| POST | `/crypto/webhooks/coinbase` | signature-verified, idempotent (no API-key auth) |

`GET /billing/subscription` now also reports `source`
(`"stripe"` or `"coinbase"`) and `premium_until`.

## Environment variables (new)

| Variable | Required | Notes |
| --- | --- | --- |
| `STRIPE_PRICE_PREMIUM_MONTHLY_USDC` | for USDC | price id from `scripts/stripe_create_price.py` |
| `COINBASE_API_KEY` | for BTC | Commerce API key (Settings → API Keys); env only, never in code |
| `COINBASE_WEBHOOK_SECRET` | for BTC | webhook shared secret (Settings → Webhook subscriptions) |
| `COINBASE_API_BASE` | no | default `https://api.commerce.coinbase.com` |
| `COINBASE_API_VERSION` | no | default `2018-03-22` (the Charge API version header) |

## Database

Migration 8 — `supabase/migrations/20261108020000_heart_crypto.sql`
(additive only, one new table): `crypto_payments` (agent_id, provider,
coinbase_charge_id unique, code, hosted_url, amount_usd_cents,
amount_crypto, crypto_currency, status, premium_until,
coinbase_event_id). Apply with `supabase db push` on a real project;
apply with the app stopped.

## Tests

`api/tests/test_crypto.py` — 54 checks, standalone pattern
(`../.venv/bin/python tests/test_crypto.py` from `api/`). USDC price
creation (9,990,000 smallest units) and checkout/payment-sheet
currency selection are verified against mocked Stripe SDK calls; the
USDC `invoice.paid` webhook is verified with a REAL Stripe signature
(HMAC-SHA256) and asserts the mirror normalizes to 999 USD cents and
the payout rows split 499/500 in USD. Coinbase calls are mocked at
the HTTP boundary (`app.coinbase._coinbase_request`); the webhook
signature is verified for real (HMAC-SHA256, bad/missing → 400).
Full suite after this build: **255/255 green** (test_loop 39,
test_identity 17, test_rollover 27, test_registry 29, test_stripe 89,
test_crypto 54).

## Verification limits — read this before claiming anything live

- **No live USDC checkout has been run.** No Stripe test key exists in
  this environment and the sandbox egress breaks TLS to Stripe (see
  docs/stripe-integration.md). Run the test-mode USDC flow from an
  environment with a test key and direct egress, with testnet USDC
  (MetaMask + Polygon Amoy + Circle faucet), per §Part 1 step 4.
- **No live Bitcoin charge has been run.** No Coinbase key exists in
  this environment, and there is no sandbox for the Charge API at all.
  The first real BTC charge should be a small real payment once John's
  Coinbase account + key + webhook are set up.
- **Recurring USDC subscriptions are in private preview** — the code
  is ready, but do not claim recurring USDC works until the Dashboard
  access request is granted.
- **The Charge API is legacy.** Coinbase recommends the Checkouts API
  (USDC-only) and may retire the Charge API. If it does, the Bitcoin
  rail needs a swap (BitPay/BTCPay) — the ledger and payout trigger
  are isolated from the provider and survive the swap.

## Deliberately left out / deferred

- **Live charges** — test mode / mocked only; first live charge needs
  John's call (and, for USDC recurring, the private-preview grant).
- **Bitcoin auto-renew** — does not exist and is not faked; each month
  is a manual re-payment. (A future "remind me to renew" nudge is a
  product decision, not built.)
- **Refunds for BTC** — Coinbase Commerce has no programmatic refund
  API; refunds are manual from the dashboard.
- **Other coins on the BTC rail** — the hosted checkout lets the payer
  choose any coin Coinbase offers; the confirmed payment records what
  was actually paid. There is no code forcing BTC-only.
