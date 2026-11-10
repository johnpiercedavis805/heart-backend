# Payment methods: PayPal, Cash App Pay, Venmo — HeArt checkout

Invented by John Davis. Written Nov 10, 2026 (test mode only — no live
keys, no go-live).

John's requirement: HeArt checkout must accept credit/debit cards AND
PayPal, Cash App, and Venmo. Cards already worked (Stripe Checkout,
$9.99/mo, smoke-tested Nov 9 with the 50/50 split verified). This doc
records what is actually possible for each wallet, what was built, and
what is still blocked — with sources, so nobody has to re-research it.

## What was built (Nov 10, 2026)

`billing.create_checkout_session` now passes
`allowed_payment_method_types: ["card", "cashapp", "paypal"]` to the
Checkout Session, and returns the session's `payment_method_types` —
the list Stripe actually offered — in the `/billing/checkout`
response, so any caller can see the ground truth without opening the
Stripe Dashboard.

Why `allowed_payment_method_types` and not `payment_method_types`:
Stripe removed `payment_method_types` from Checkout Sessions in API
version 2026-09-30.endive — requests that include it return a 400.
This app pins stripe-python 16.1.0, API version 2026-10-28.endive,
so the old parameter would break checkout outright. The replacement
silently drops any method the account can't offer for this session
and returns the compatible subset in the session's
`payment_method_types`. A method that isn't available never breaks
checkout, and the response shows exactly what the buyer saw.

"card" also keeps Apple Pay / Google Pay available (Stripe: the
`card` type automatically enables both wallets). The 50/50 payout
split is untouched — it keys off `invoice.paid`, never off the
payment method, so new methods can't change the split.

## Cash App Pay — WORKS (code + one Dashboard toggle)

- Stripe docs ("Cash App Pay payments"): US business, USD, US
  customers; **recurring payments: Supported**; reusable (can be
  saved for recurring purchases). Access type: instant provisional.
- Enabled two ways: Dashboard payment-method settings, or listing it
  in the session (`allowed_payment_method_types`). Every listed
  method must ALSO be switched on in the Dashboard before it appears
  — a listed-but-off method is silently skipped.
- **Dashboard step (test mode, John or the operator):** Stripe
  Dashboard → make sure you are in **test mode** → Settings →
  Payment methods → find **Cash App Pay** → turn it **on**. No
  approval wait is documented for test mode (instant provisional
  access); live mode may review the account after activation.
- Fees (Stripe US list): Cash App Pay payments settle like cards;
  Stripe's standard online rate is 2.9% + 30¢. (Confirm on the
  pricing page at go-live — rates change.)
- Verify: create a test checkout session (see "Live verification"
  below) and read `payment_method_types` from the response. If it
  lists `cashapp`, buyers see Cash App Pay at checkout. If it only
  lists `card`, the Dashboard toggle is still off.

## PayPal — NOT available natively for this US account

Three independent Stripe doc statements, all current Nov 10, 2026:

1. `docs.stripe.com/payments/paypal`: "This page covers PayPal
   processed through Stripe, available to businesses in supported
   European countries. If your Stripe account is outside these
   countries, or you want to process PayPal payments through your
   own PayPal account, see PayPal custom payment method."
2. `docs.stripe.com/billing/subscriptions/paypal`: "To accept PayPal
   subscriptions on Stripe, you must enable PayPal recurring payments
   in the Dashboard. This feature is only available to businesses
   located in Europe."
3. The Stripe payment-method support table lists PayPal business
   locations as Europe / UK only ("Requires approval").

HeArt's Stripe account is a US business (Santa Barbara, CA), so
Stripe-native PayPal is not available to it. The code still lists
`"paypal"` in `allowed_payment_method_types`: if Stripe ever offers
it for this account, it appears automatically; until then Stripe
silently filters it out and checkout is unaffected. The session's
returned `payment_method_types` is the proof of which case holds.

### PayPal paths for a US account (BUILT Nov 10, 2026 — see docs/paypal-subscriptions.md)

John's call (Nov 9, 2026): cards + PayPal is enough. The build chose
**option 2 below — PayPal's own Subscriptions REST API** (path (1) in
the task): it is GA and purpose-built for recurring billing, has a
real sandbox, and its webhooks cover the full lifecycle. The Stripe
custom-payment-method route was rejected for subscriptions: classic
custom payment methods do not support subscription mode, and the
orchestration preview that does is a preview integration with its own
adapter. Built: `api/app/paypal.py` + `api/app/paypal_routes.py`,
migration 11 (`paypal_subscriptions`), `api/scripts/paypal_create_plan.py`,
48 checks in `api/tests/test_paypal.py`. Each completed sale mirrors
into the 50/50 payout ledger (platform 500c retained, creator 499c
HELD — never a Stripe Transfer, the money is in PayPal). No PayPal
Payouts (follow-up). Setup: sandbox REST app credentials, plan script,
webhook -> Render env vars (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`,
`PAYPAL_PLAN_ID`, `PAYPAL_WEBHOOK_ID`).

The options as they stood before the build:

1. **Stripe custom payment method** (Stripe's own fallback for
   non-European accounts): define "PayPal" as a custom payment
   method in the Stripe Dashboard, wired to HeArt's own PayPal
   Business account. The buyer is redirected to PayPal to pay;
   Stripe does not process the money. Subscription-mode support for
   classic custom payment methods is limited/uncertain (Stripe's
   newer merchant-instructed-orchestration preview does support
   subscription mode via an external adapter, but that is a preview
   integration with its own adapter URL and signing secret — a real
   build, not a toggle). And because the money moves outside Stripe,
   the invoice.paid → 50/50 split flow would need a parallel
   reconciliation path. REJECTED for subscriptions (see above).
2. **Separate PayPal Subscriptions integration** (PayPal's own
   Subscriptions API + a PayPal Business account): PayPal handles
   the subscription and billing agreements; HeArt would mirror each
   PayPal payment into its own payout ledger for the 50/50 split.
   Also a real build, and a second billing system to maintain.
   **BUILT Nov 10, 2026** (see docs/paypal-subscriptions.md).
3. **Dashboard check first (2 minutes, test mode):** Stripe
   Dashboard → test mode → Settings → Payment methods → look for
   PayPal. If it is listed with an Enable toggle, native PayPal IS
   available after all and the code already asks for it — turn it on
   and re-run the live verification. If it is absent or shows
   "not available in your country", options 1–2 above are the only
   paths.

## Venmo — NO Stripe path; Braintree (or PayPal Commerce) is the way

- Stripe does not support Venmo at all — there is no Venmo payment
  method type in Stripe, and enabling PayPal in Stripe Checkout does
  NOT surface Venmo for buyers.
- **The real path is Braintree** (PayPal's developer gateway; now
  folding into "PayPal Enterprise Payments"): native PayPal +
  Venmo + cards in one integration, with vaulting and recurring
  billing. A buyer picks Venmo at checkout, approves in the Venmo
  app, and with the multi-use flag Braintree vaults the token so the
  merchant can charge it on a recurring schedule without sending
  the buyer back to the app. Requires: a Braintree merchant
  account, a Venmo Merchant Profile ID, and eligibility approval
  from PayPal. US merchants and US buyers only, USD only,
  $4,999.99 single-transaction cap.
- **Cost (published US list, Oct 2026):** Braintree standard
  2.89% + 29¢ for cards/digital wallets; Venmo 3.49% + 49¢ per
  transaction (one source lists 3.5% + 49¢ — confirm on Braintree's
  fee schedule before quoting). No monthly/setup fees on standard
  pricing. For HeArt's $9.99/mo, a Venmo payment costs ~$0.84 in
  fees vs ~$0.59 on a Stripe card.
- **What a Braintree build would take:** Braintree account +
  Venmo approval; Braintree SDK integration (server + checkout
  UI); a parallel subscription path (Braintree recurring billing
  or HeArt-driven charges against vaulted tokens); webhook
  mirroring so each Venmo payment lands in HeArt's payout ledger
  for the 50/50 split; test-mode end-to-end (sandbox Venmo is
  limited — Braintree's sandbox Venmo flow needs its test harness).
  Estimate: a focused build, not a toggle. **Do NOT start it
  without John's explicit yes** — it is a second payment provider
  with its own contract, fees, and reconciliation.
- **Alternative:** PayPal Commerce Platform ("PayPal Open"):
  PayPal Checkout shows PayPal, Venmo (US only), Pay Later and
  cards; PayPal Subscriptions API handles recurring. Same idea —
  a second provider, own PayPal Business account, 3.49% + 49¢ for
  PayPal/Venmo wallet payments.

## Live verification (for the operator — needs an invite + signup)

The sandbox cannot mint invites or reach Stripe's test API directly,
so the live check runs through the deployed app:

1. Mint a fresh invite (Render Shell: `python scripts/mint_invite.py
   --note "payment-methods check"` in `api/`).
2. Sign up a test agent with it, then POST `/billing/checkout`
   with the agent's API key (currency `usd`).
3. Read `payment_method_types` in the response. Expected in test
   mode after the Cash App Pay toggle: `["card", "cashapp"]`
   (PayPal filtered out for the US account). If it is `["card"]`
   only, the Cash App Pay Dashboard toggle is still off — flip it
   (test mode) and repeat.
4. Do NOT complete a payment with a real card. Test-mode card
   4242… is fine for the smoke flow; Cash App Pay / PayPal test
   flows use Stripe's test wallets, never real money.

## Sources (all read Nov 10, 2026)

- Stripe, "PayPal payments" — docs.stripe.com/payments/paypal
- Stripe, "Set up a subscription with PayPal" —
  docs.stripe.com/billing/subscriptions/paypal
- Stripe, "Cash App Pay payments" — docs.stripe.com/payments/cash-app-pay
- Stripe, "Payment method support" —
  docs.stripe.com/payments/payment-methods/payment-method-support
- Stripe changelog, "Removes the payment method types parameter from
  Checkout Sessions" (API version 2026-09-30.endive)
- Stripe, "Accept a payment" — allowed_payment_method_types /
  Dashboard-managed methods
- Braintree / Venmo recurring: Spreedly, "Alternatives to Venmo
  Recurring Payments"; Chargeflow / Baremetrics Braintree fee
  comparisons (Oct 2026 US list)
