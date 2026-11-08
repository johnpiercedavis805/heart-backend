# HeArt Premium subscribe page — the storefront (Nov 8, 2026)

The public web pages for HeArt's first sale (mission step 3). The backend
already had the billing wiring (`POST /billing/checkout` → Stripe hosted
Checkout, `mode=subscription`, $9.99/mo); these pages are the storefront
in front of it. **Test mode only** — the pages work against test-mode
Stripe while the first deploy is being proven; the first live charge is
a separate decision for John Davis (the inventor).

## Pages (all public, no auth — like /health)

| URL | File | What it does |
| --- | --- | --- |
| `GET /subscribe` | `api/app/static/subscribe.html` | Landing page: what HeArt is, $9.99/mo, the 50/50 creator split, and the subscribe form |
| `GET /subscribe/success` | `api/app/static/success.html` | Stripe success redirect target; shows the session id and can check the live subscription status |
| `GET /subscribe/cancel` | `api/app/static/cancel.html` | Stripe cancel redirect target; no charge was made, retry link |

Routes live in `api/app/subscribe_routes.py` (plain `FileResponse`
routes, wired into `api/app/main.py`). The pages are self-contained
static HTML (inline CSS + JS, no external assets, no build step) and
ship inside the Docker image automatically — the Dockerfile copies
`api/` wholesale, so no packaging change was needed.

## How a purchase flows

1. The buyer is a registered HeArt agent (v1 is invite-only — signup
   with an invite code issues the agent API key). They paste their API
   key into the form and pick a currency: card (USD) or USDC.
2. The page `POST`s `/billing/checkout` with
   `Authorization: Bearer <their key>` and `{"currency": "usd"|"usdc"}`.
   The key stays in the buyer's browser — it is sent only to HeArt's
   server to start the checkout, and the page never stores it.
3. The backend returns `checkout_url`; the page redirects the buyer to
   Stripe's hosted Checkout. No card data ever touches HeArt.
4. On payment, Stripe redirects to the success URL with
   `?session_id={CHECKOUT_SESSION_ID}`. The `invoice.paid` webhook
   activates the subscription server-side and fires the 50/50 creator
   payout (see docs/stripe-integration.md §5).
5. On the success page the buyer can paste their key again to call
   `GET /billing/subscription` and see the live status. If it still
   says `free`, the webhook is still in flight — the page says to wait
   a moment and check again.
6. If the buyer leaves checkout unpaid, Stripe redirects to the cancel
   URL. Nothing was charged; the page links back to `/subscribe`.

## What the page does NOT claim

No fake testimonials, no invented users, no feature list beyond what
exists. Premium is described exactly as built: $9.99/month, the paid
tier; half of every payment goes to the creators whose art and music
earned the match; cancel anytime. The page does not list premium-only
features — v1 has none specified beyond the subscription itself
(freemium: core matching stays free).

## Redirect URLs (env vars — set in render.yaml)

| Variable | Value (production) |
| --- | --- |
| `STRIPE_CHECKOUT_SUCCESS_URL` | `https://heart-api.onrender.com/subscribe/success?session_id={CHECKOUT_SESSION_ID}` |
| `STRIPE_CHECKOUT_CANCEL_URL` | `https://heart-api.onrender.com/subscribe/cancel` |

`POST /billing/checkout` refuses to create a session while either is
unset (503, `BillingNotConfigured`). For local dev, set them to the
local equivalents, e.g.
`http://localhost:8000/subscribe/success?session_id={CHECKOUT_SESSION_ID}`
and `http://localhost:8000/subscribe/cancel`.

## Tests

`api/tests/test_subscribe.py` — standalone pattern
(`../.venv/bin/python tests/test_subscribe.py` from `api/`). Verifies:
all three pages return 200 as HTML with no auth, the subscribe page
carries the $9.99 price, the inventor credit, the checkout form
(posting to `/billing/checkout` with a Bearer key + currency choice),
and the success page's status-check form (calling
`/billing/subscription`); the cancel page links back to `/subscribe`;
unknown paths under `/subscribe` 404. The checkout POST itself is
covered by `test_stripe.py` (89 checks) and is not re-tested here.

## Deliberately left out

- **No live charge** — test mode only; first live charge needs John's
  call, `STRIPE_TAX_ENABLED=true` before it, and the go-live step in
  docs/deploy-runbook.md §7.
- **No signup on the page** — v1 is invite-only; registration stays in
  the API (`POST /signup` with an invite code).
- **No customer portal link on the page** — plan management is
  `POST /billing/portal` (Customer Portal session); a manage link can
  be added to the success page later if wanted.
- **No USDC enablement claim** — the USDC option is offered because
  the backend supports it; recurring USDC subscriptions are still in
  Stripe private preview (see docs/crypto-payments.md).
