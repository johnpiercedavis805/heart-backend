# HeArt — Payout map: where the money lands

John's order (Nov 9, 2026): make as much money as possible, sent to his
**Chime, PayPal, and Cash App**. This doc is the honest map: what lands
where today, what each destination needs, and what does NOT exist.

**Numbers today (Nov 9, 2026, evening): $0 collected, 0 subscribers,
0 payouts.** Nothing below has moved money yet. Say so plainly until it
does.

## The split (both rails, same math)

Every $9.99/month payment splits 50/50 in the ledger:

- **Platform (HeArt / John): 500¢ retained**
- **Creator(s) the subscriber matched with: 499¢ held** (two creators:
  249¢ each, HeArt 501¢)

"Retained" and "held" are ledger words. The creator half is **held,
never auto-sent** — for humans it moves only via a Stripe Connect
Transfer once they have a Connect account; for AIs it is never
transferred at all (held by registry serial). PayPal creator shares
are held the same way and never become Stripe Transfers (the money
never touched Stripe).

## Rail 1 — Stripe (LIVE Nov 9, 2026)

- **What it takes:** card (live, verified) + Cash App Pay (code live;
  the Dashboard toggle flip is in progress via a browser task —
  verify in Stripe Dashboard → Settings → Payment methods before
  claiming it is on).
- **Fee:** ~$0.59 per $9.99 card payment (2.9% + 30¢). Cash App Pay
  through Stripe is priced like a card payment — confirm the exact
  rate in the Dashboard before quoting it.
- **Where the money lands:** Stripe balance → **payouts to the linked
  bank: John's Chime** (The Bancorp Bank, ending 3487). Standard
  Stripe payout timing is 2 business days rolling (first payout on a
  new account can take 7–14 days — confirm in Dashboard → Settings
  → Payouts). Nothing to build: payouts are already configured.
- **John's share:** arrives in Chime automatically on the payout
  schedule. HeArt's retained 500¢ per payment is part of the same
  payout; the creator 499¢ is held in the ledger until a transfer
  is made.
- **Tax:** automatic tax is ON (`STRIPE_TAX_ENABLED=1`). Stripe
  collects/remits; the $9.99 price is tax-exclusive (tax is added
  on top at checkout where applicable). The Nov 9 live test found
  and fixed the address bug (checkout now collects the buyer's
  address for tax).

## Rail 2 — PayPal (SANDBOX done; LIVE not yet)

- **Sandbox (done Nov 9):** app "HeArt Sandbox", plan
  P-4EB341546J058620BNLZIY4Q ($9.99/mo, ACTIVE), webhook
  3R743904KV778531N, all four `PAYPAL_*` env vars in Render,
  deploy live, /health 200. Sandbox verification test running.
- **Where the money lands:** PayPal subscription money lands in
  **HeArt's PayPal account balance** — the account the REST app
  belongs to. Fee: 3.49% + 49¢ per payment ≈ **$0.84 on $9.99**
  (vs ~$0.59 Stripe card). The ledger mirrors each sale into the
  same 500/499 split; the creator half stays held in PayPal.
- **John's share → Chime:** PayPal balance → withdraw to his linked
  bank (Chime) from the PayPal app/website. Standard transfer is
  free (1–3 business days); instant transfer carries a fee. This is
  a manual step John does (or an operator does) — there is no
  automatic PayPal → Chime sweep built, and none is planned
  without his yes.
- **John's share → PayPal (staying put):** it is already there.
  Nothing to do — the balance IS his PayPal money.
- **Creators' held PayPal shares:** need PayPal Payouts (API) +
  creator PayPal emails + John's yes. Not built. Do not promise
  creator PayPal payouts until that is built and tested.

## Rail 3 — Cash App (NO automated rail — be honest)

- **There is no automated payout rail from Stripe or PayPal to Cash
  App.** Neither Stripe nor PayPal can push money to a Cash App
  account. Do not build a fake one and do not promise one.
- **The real path is manual and short:** money lands in Chime
  (Stripe payouts) or PayPal balance → John (or the operator) moves
  it with **Cash App "Add Cash" from his Chime debit card** (instant,
  from the Chime balance). PayPal → Chime first if the money is in
  PayPal, then Chime → Cash App. Minutes, not days, once the money
  is in Chime.
- **Cash App Pay (inbound) is different from Cash App (outbound):**
  buyers CAN pay with Cash App Pay at Stripe checkout (once the
  live toggle is on). That is money coming IN through Stripe, and
  it lands in Chime like any Stripe payment. It does not send money
  OUT to John's Cash App.

## PayPal LIVE swap — readiness checklist

Sandbox is done. These are the remaining steps before PayPal goes
live. Nothing here is done yet.

1. **Which PayPal account? — NEEDS JOHN.** The sandbox app is under
   **guitarjohn805@gmail.com**, not johnpiercedavis805@gmail.com.
   The live REST app must be created under the PayPal account John
   wants the money to land in. If that is a different account, the
   live app, plan, and webhook all live under that account. Also:
   a live REST app needs a **PayPal Business account** — a Personal
   account cannot take live subscription payments. If his account
   is Personal, the upgrade to Business is a John step (PayPal
   prompts for it; it is his account, his business details).
2. **Live REST app:** Developer Dashboard (LIVE mode) → Apps &
   Credentials → Create App → Client ID + Secret → Render env vars
   `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`. Never in repo/chat.
3. **Live plan:** `api/scripts/paypal_create_plan.py` is
   sandbox-only by design. Use the live-mode variant (see
   docs/paypal-subscriptions.md) or create the $9.99/mo plan in the
   Dashboard. → `PAYPAL_PLAN_ID`.
4. **Live webhook:** same app → Webhooks → URL
   `https://heart-api-ifsr.onrender.com/paypal/webhooks`, the same
   five events → `PAYPAL_WEBHOOK_ID`.
5. **Env flip:** `PAYPAL_TEST_MODE_ONLY=0`,
   `PAYPAL_API_BASE=https://api-m.paypal.com`, redeploy, /health 200.
6. **Live verification:** a real $9.99 sandbox-style test is NOT
   possible on live without a real charge — a live test purchase
   needs John's explicit approval for that specific charge. Until
   then, verify the live route with a Checkout-less check (webhook
   endpoint reachable, plan ID resolves) and the first real buyer.

## What money CANNOT do (guardrails)

- No price changes ($9.99 is John's).
- No creator payouts (Stripe Transfer or PayPal Payouts) without
  John's explicit yes per mechanism.
- No automatic sweep to Cash App — it does not exist.
- No live PayPal until sandbox verifies AND the account question
  (step 1 above) is answered.
- Secrets live in Render env vars only.
