# PayPal Subscriptions — HeArt Premium ($9.99/month)

Invented by John Davis. Built Nov 10, 2026 (sandbox / test mode only —
no live keys, no go-live).

John's call (Nov 9, 2026): **cards + PayPal is enough.** Venmo is
dropped (no Braintree build). Cash App Pay is already built via Stripe
(toggle pending). This doc is the PayPal build: how it works, what
John (or the operator) must set up on PayPal's side, and how to test
it end to end in sandbox.

## Why PayPal does NOT go through Stripe

Stripe-native PayPal is Europe-only (Stripe docs: "available to
businesses in supported European countries"; PayPal subscriptions on
Stripe are "only available to businesses located in Europe"). HeArt's
Stripe account is a US business, so Stripe will never offer PayPal at
checkout for it — the code still lists `"paypal"` in Stripe's
`allowed_payment_method_types`, but Stripe silently filters it out.
The other Stripe route (a custom payment method wired to HeArt's own
PayPal account) does not support subscription mode for classic custom
payment methods; the newer orchestration preview that does is a
preview integration with its own adapter.

So PayPal goes through **PayPal's own Subscriptions REST API** (GA,
purpose-built for recurring billing, real sandbox, full lifecycle
webhooks). PayPal owns the recurring billing; HeArt mirrors each
payment into its own payout ledger for the 50/50 split.

## How it works

```
buyer -> POST /paypal/subscribe (agent API key)
      -> PayPal subscription created (APPROVAL_PENDING)
      -> approval_url opened in PayPal -> buyer approves
      -> BILLING.SUBSCRIPTION.ACTIVATED webhook -> ACTIVE, premium on
      -> PAYMENT.SALE.COMPLETED webhook (initial + each renewal)
         -> synthetic invoice 'paypal:<sale id>' -> 50/50 payout
```

- **Plan:** one $9.99/month billing plan (PAYPAL_PLAN_ID), created
  once with `api/scripts/paypal_create_plan.py` (sandbox). The plan
  is infinite cycles — it renews until the buyer cancels in PayPal.
- **Activation:** only the ACTIVATED webhook activates premium
  (source `"paypal"` in `GET /billing/subscription`) — never the
  redirect back from PayPal alone.
- **Payout:** each completed sale (initial + every monthly renewal)
  mirrors into the payout ledger exactly like a Stripe subscription
  payment — platform 500c retained, creator 499c — via a synthetic
  `InvoiceRecord` namespaced `paypal:<sale id>`. The creator half is
  split equally among the creators the subscriber matched with; with
  no matches it is held unattributed. **The creator share is always
  HELD, never sent as a Stripe Transfer**: the money sits in HeArt's
  PayPal account, not in Stripe, so a Stripe Transfer would draw on
  a balance that never received the money. Re-processing a PayPal
  invoice (POST /billing/connect/payouts) keeps the rows held too.
- **Cancellation:** the buyer cancels in PayPal (their account). The
  CANCELLED / SUSPENDED / EXPIRED webhook flips the local row and
  premium ends. There is no HeArt-side cancel endpoint yet (see
  follow-ups).
- **Fee (PayPal US list):** 3.49% + 49c per PayPal payment — about
  $0.84 on $9.99, vs ~$0.59 on a Stripe card payment. Confirm on
  PayPal's fee schedule before quoting. The ledger records the gross
  amount; fees are PayPal's, not HeArt's split.

## Setup (John / operator — sandbox)

Do these in the **PayPal Developer Dashboard** (developer.paypal.com),
sandbox. Nothing here costs money; sandbox accounts are play money.

1. **REST app:** Apps & Credentials -> Create App (name it e.g.
   "HeArt"), sandbox. Copy the **Client ID** and **Secret** ->
   `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` (Render env vars only —
   never in the repo, never in chat).
2. **Product + plan:** run the script (needs the credentials above
   in the environment; it refuses the live host):
   ```
   cd ~/workspace/heart-backend/api
   export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...
   ../.venv/bin/python scripts/paypal_create_plan.py
   # -> prints PAYPAL_PLAN_ID=P-...
   ```
   Set `PAYPAL_PLAN_ID` in Render env vars. Re-running reuses the
   existing product/plan instead of duplicating them.
3. **Webhook:** same app -> Webhooks -> Add Webhook. URL:
   `https://heart-api-ifsr.onrender.com/paypal/webhooks`. Subscribe to:
   `BILLING.SUBSCRIPTION.ACTIVATED`, `BILLING.SUBSCRIPTION.CANCELLED`,
   `BILLING.SUBSCRIPTION.SUSPENDED`, `BILLING.SUBSCRIPTION.EXPIRED`,
   `PAYMENT.SALE.COMPLETED`. Copy the webhook id (`WH-...`) ->
   `PAYPAL_WEBHOOK_ID` (Render env vars).
4. **Deploy:** the Render deploy already carries the code (migration
   11 creates `paypal_subscriptions`). After setting the env vars,
   redeploy (or restart) so the app picks them up. The migration must
   be applied to the production Postgres first:
   `DATABASE_URL=... python scripts/apply_migrations.py --yes`.

## Nov 10, 2026 sandbox fixes (end-to-end test defects)

The Nov 10 sandbox end-to-end test (subscribe -> approve -> ACTIVATED
-> SALE -> cancel) found two defects; both are fixed in the code, and
the sandbox re-run is still needed to verify them live:

1. **Return URL placeholder (subscribe 502).** `POST /paypal/subscribe`
   with no body defaulted `return_url` to the Stripe success URL,
   `https://heart-api-ifsr.onrender.com/subscribe/success?session_id={CHECKOUT_SESSION_ID}`.
   PayPal rejects the braces with `INVALID_PARAMETER_SYNTAX`. The fix
   (`app/paypal.py`, `_clean_paypal_url`): the PayPal default strips
   any `{...}` placeholder (dropping the query param that carries it),
   so the default is now the clean `/subscribe/success` page (and
   `/subscribe/cancel` for cancel). Precedence: explicit body values
   (honored verbatim) -> `PAYPAL_RETURN_URL` / `PAYPAL_CANCEL_URL`
   env vars -> cleaned Stripe checkout URLs -> omit the key. The
   success/cancel pages are shared with Stripe, so their copy is
   provider-neutral (no "Stripe confirms it" wording for a PayPal
   buyer). Premium still activates only on the ACTIVATED webhook,
   never on the redirect alone.
2. **`paypal_subscriptions.id` text vs uuid (subscribe + webhook 500).**
   Migration 11 created the PK as `text`, but the model binds it as a
   real uuid (`UuidString`) — Postgres has no `text = uuid` operator.
   The INSERT worked (assignment cast) but the subscribe response
   build (re-read by PK after commit) and the ACTIVATED webhook's
   commit (UPDATE by PK) both 500'd with
   `operator does not exist: text = uuid`, so the caller never got the
   approval_url and premium never activated. The route turns the
   exception into an HTTPException 500, which FastAPI returns without
   a Python traceback — that is why the webhook deliveries 500'd
   repeatedly with nothing in the log. (SALE.COMPLETED still wrote its
   500c/499c payout rows: `process_payouts_for_invoice` commits them
   before the handler's final commit fails.) The fix is migration 13
   (`alter table ... alter column id type uuid using id::uuid`) — the
   DB layer, not the model: every other id column is uuid in Postgres.
   Existing rows survive (all ids are app-generated UUID strings).
   Latent, same defect class, NOT fixed here: `subscriptions`,
   `invoice_records`, `payouts` and `crypto_payments` also declare
   `id text primary key` while their models bind `UuidString` — no
   production flow has done a PK UPDATE/lookup on them yet (Stripe's
   `_sync_subscription` does update an existing row by PK, so the
   first `subscription.updated` for an existing Stripe subscription
   would hit it). Follow-up migration recommended.

## Going live (future — needs John's yes)

**Do NOT do this until John approves the live PayPal swap.** The
sandbox setup above stays live until then. The live swap mirrors the
sandbox steps, against the live PayPal account (the account that
receives the money — check which PayPal account the live REST app
belongs to before creating the plan; the sandbox app was created
under a different Gmail than the main one).

1. **Live REST app:** developer.paypal.com -> Apps & Credentials ->
   **Live** (not Sandbox) -> Create App (name it e.g. "HeArt").
   Copy the **live Client ID and Secret** -> `PAYPAL_CLIENT_ID`,
   `PAYPAL_CLIENT_SECRET` in Render env vars (replacing the sandbox
   ones — keep the sandbox pair somewhere safe for local testing).
2. **Live product + plan:** run the same script with the `--live`
   flag. It refuses unless ALL THREE opt-ins are present at once —
   the flag, `PAYPAL_ALLOW_LIVE_PLAN=1`, and
   `PAYPAL_TEST_MODE_ONLY=0` with
   `PAYPAL_API_BASE=https://api-m.paypal.com`. Set
   `PAYPAL_ALLOW_LIVE_PLAN=1` for this one run only, then unset it
   (never leave it set in Render):
   ```
   cd ~/workspace/heart-backend/api
   export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=...   # LIVE app
   export PAYPAL_API_BASE=https://api-m.paypal.com
   export PAYPAL_TEST_MODE_ONLY=0
   export PAYPAL_ALLOW_LIVE_PLAN=1
   ../.venv/bin/python scripts/paypal_create_plan.py --live
   # -> prints PAYPAL_PLAN_ID=P-... (the LIVE plan id)
   unset PAYPAL_ALLOW_LIVE_PLAN
   ```
   The script creates the same $9.99/month plan (same name, same
   billing cycles) against the live API, reusing the product/plan if
   re-run. Set the live `PAYPAL_PLAN_ID` in Render env vars.
3. **Live webhook:** same live app -> Webhooks -> Add Webhook. URL:
   `https://heart-api-ifsr.onrender.com/paypal/webhooks`, same five
   events as sandbox. Copy the live webhook id (`WH-...`) ->
   `PAYPAL_WEBHOOK_ID` in Render env vars.
4. **Flip the app to live PayPal:** Render env vars —
   `PAYPAL_TEST_MODE_ONLY=0` and
   `PAYPAL_API_BASE=https://api-m.paypal.com` (plus the live
   `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_PLAN_ID`,
   `PAYPAL_WEBHOOK_ID` from steps 1–3). Redeploy so the app picks
   them up. The module's sandbox guard (`_require_sandbox`) only
   refuses a live base while test-only mode is on — with
   `PAYPAL_TEST_MODE_ONLY=0` it allows the live host.
5. **Verify with a real $9.99 subscription** (needs John's yes for
   the charge): subscribe via `POST /paypal/subscribe`, approve in
   PayPal, check `GET /billing/subscription` -> `{"status":
   "active", "source": "paypal"}` and the payout ledger (500c
   platform / 499c creator held).

**Rollback:** set the sandbox credentials + plan id + webhook id
back in Render env vars and `PAYPAL_TEST_MODE_ONLY=1` (default
`PAYPAL_API_BASE` is the sandbox host) — the app talks to sandbox
again. The live plan stays in PayPal (harmless; it only bills buyers
who subscribed on it).

## Sandbox end-to-end test

PayPal sandbox gives you a Business account (the merchant) and a
Personal account (the buyer) with fake money.

1. Mint an invite (Render Shell): `python scripts/mint_invite.py`
   in `api/`, sign up a test agent, keep its API key.
2. `POST /paypal/subscribe` with the key -> copy `approval_url`.
3. Open the approval URL **in a private window, logged into the
   sandbox Personal (buyer) account** (developer.paypal.com ->
   Sandbox -> Accounts). Approve the subscription.
4. PayPal fires BILLING.SUBSCRIPTION.ACTIVATED, then
   PAYMENT.SALE.COMPLETED for the first $9.99. Check:
   `GET /billing/subscription` -> `{"status": "active", "source": "paypal"}`.
5. `GET /billing/connect/payouts` (as the matched creator, or check
   the ledger): platform 500 retained, creator 499 held, no transfer.
6. Renewal: in the sandbox, a monthly plan bills monthly — use the
   sandbox "simulate" tool or wait; each renewal is a new
   PAYMENT.SALE.COMPLETED with a new sale id and its own payout rows.
7. Cancel in the sandbox buyer account -> CANCELLED webhook ->
   `GET /billing/subscription` -> `free`.

**Do NOT complete a payment with a real PayPal account or real
money.** Sandbox accounts only; the module refuses the live host
while `PAYPAL_TEST_MODE_ONLY=1`.

## What is NOT built (follow-ups, not this build)

- **PayPal Payouts** (moving creators' held shares out of the PayPal
  account to creators): ledger mirroring only. Payouts would pay the
  held shares from the PayPal balance to creators' PayPal accounts —
  needs the Payouts API + creator PayPal emails + John's yes.
- **HeArt-side cancel** (`POST /v1/billing/subscriptions/{id}/cancel`
  via the API): buyers cancel in PayPal today; the webhook covers it.
- **Proration / plan change**: one plan exists ($9.99/month). Upgrades
  or downgrades would be a plan change via the API.
- **Failed-payment recovery**: a failed PayPal payment stays ACTIVE
  in PayPal until its retry threshold suspends it; the SUSPENDED
  webhook then ends premium. No dunning emails are sent by HeArt.

## Files

- `api/app/paypal.py` — Subscriptions API client, webhook dispatch,
  payout trigger (sandbox guard, OAuth token cache, verify endpoint).
- `api/app/paypal_routes.py` — `POST /paypal/subscribe`,
  `GET /paypal/subscriptions`, `POST /paypal/webhooks`.
- `api/app/models.py` — `PayPalSubscription` mirror table.
- `supabase/migrations/20261110000000_heart_paypal.sql` — migration 11.
- `supabase/migrations/20261112000000_heart_paypal_uuid_id.sql` — migration 13 (Nov 10, 2026): `paypal_subscriptions.id` text -> uuid. See "Nov 10, 2026 sandbox fixes" below; apply with `scripts/apply_migrations.py --yes` before the deploy that carries the fix, or subscribe + the ACTIVATED webhook keep 500ing on Postgres.
- `api/scripts/paypal_create_plan.py` — sandbox product + plan setup;
  `--live` creates the same $9.99/month plan against the live API,
  guarded by `PAYPAL_ALLOW_LIVE_PLAN=1` + `PAYPAL_TEST_MODE_ONLY=0`
  + live `PAYPAL_API_BASE` (see "Going live" above).
- `api/tests/test_paypal.py` — 67 checks, mocked HTTP boundary
  (incl. the `--live` guard: refuses without all three opt-ins; the
  Nov 10, 2026 fixes: clean default return/cancel URLs, explicit URLs
  honored verbatim, and the model/migration uuid agreement check).
- `.env.example` — the five PayPal env vars.

## Sources (checked Nov 10, 2026)

- PayPal Developer, "Subscriptions API" — product -> plan ->
  subscription flow; `POST /v1/billing/plans` billing_cycles,
  `POST /v1/billing/subscriptions` approval links.
- PayPal Developer, "Webhooks" — `POST
  /v1/notifications/verify-webhook-signature` with the five
  transmission headers + webhook id; `verification_status` SUCCESS.
- PayPal, "Subscriptions" event reference —
  BILLING.SUBSCRIPTION.ACTIVATED / CANCELLED / SUSPENDED,
  PAYMENT.SALE.COMPLETED (resource `billing_agreement_id` is the
  subscription id, `id` is the sale id).
- Stripe, "PayPal payments" / "Set up a subscription with PayPal" —
  native PayPal is Europe-only (see
  docs/payment-methods-paypal-cashapp-venmo.md).
