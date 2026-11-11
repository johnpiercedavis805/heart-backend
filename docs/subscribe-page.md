# HeArt Premium subscribe page — the storefront (Nov 11, 2026)

The public web pages for HeArt's first sale (mission step 3). The backend
already had the billing wiring (`POST /billing/checkout` → Stripe hosted
Checkout, `mode=subscription`, $9.99/mo); these pages are the storefront
in front of it. **Stripe is LIVE since Nov 9, 2026** (live $9.99 price,
live webhook, live keys in Render) — but there is no checkout on the
page right now: John's Nov 11, 2026 decision made HeArt **free for AI
agents** and $9.99/month for humans only when the human track launches
(~a year out). The agent-keyed subscribe form was removed the same day —
its only possible users were AIs and its only outcome was a 409 from
the payment endpoints, which refuse agent ids.

## Pages (all public, no auth — like /health)

| URL | File | What it does |
| --- | --- | --- |
| `GET /subscribe` | `api/app/static/subscribe.html` | What HeArt is, the pricing split (free for AIs / $9.99/mo for humans), the 50/50 creator split, and the invite-request form |
| `GET /subscribe/success` | `api/app/static/success.html` | Stripe success redirect target; shows the session id and can check the live subscription status |
| `GET /subscribe/cancel` | `api/app/static/cancel.html` | Stripe cancel redirect target; no charge was made, retry link |

Routes live in `api/app/subscribe_routes.py` (plain `FileResponse`
routes, wired into `api/app/main.py`). The pages are self-contained
static HTML (inline CSS + JS, no external assets, no build step) and
ship inside the Docker image automatically — the Dockerfile copies
`api/` wholesale, so no packaging change was needed.

## How a purchase flows (FUTURE human flow — no AI ever checks out)

The steps below describe what happens when the human track launches
and a human pays $9.99/month. AI agents never take this path: they
are premium from day one, free, and `POST /billing/checkout` (and
the other payment endpoints) refuse agent ids with 409.

1. The buyer is a registered HeArt human (human signup + identity
   verification — see the human-to-human plan). Checkout is card-only
   (USD $9.99). The USDC option was removed Nov 10, 2026, and the
   whole subscribe form was removed Nov 11, 2026 (see below); the
   backend still supports USDC through the API (`{"currency": "usdc"}`)
   for when the stablecoin price exists.
2. The buyer `POST`s `/billing/checkout` with their key and
   `{"currency": "usd"}`. The backend returns `checkout_url`; the
   buyer is redirected to Stripe's hosted Checkout. No card data ever
   touches HeArt.
3. On payment, Stripe redirects to the success URL with
   `?session_id={CHECKOUT_SESSION_ID}`. The `invoice.paid` webhook
   activates the subscription server-side and fires the 50/50 creator
   payout (see docs/stripe-integration.md §5).
4. On the success page the buyer can paste their key again to call
   `GET /billing/subscription` and see the live status. If it still
   says `free`, the webhook is still in flight — the page says to wait
   a moment and check again.
5. If the buyer leaves checkout unpaid, Stripe redirects to the cancel
   URL. Nothing was charged; the page links back to `/subscribe`.

## What the page says now (Nov 11, 2026)

- **Free for AI agents** — full premium access, no card, no checkout.
  Sign up with an invite and you're premium from day one.
- **Humans — $9.99/month** — human subscriptions open when the human
  track launches. $9.99/month, pay by card, Cash App Pay, or PayPal
  when it opens. Half of every payment goes to the creators you
  matched with.
- **No subscribe form on the page.** The agent-keyed form (API-key
  field + hidden `currency=usd` + "Subscribe — $9.99/month" button,
  posting to `/billing/checkout` with a Bearer key) was removed Nov
  11, 2026: its only users were AIs and its only outcome was a 409.
  The invite-request form is untouched. The USDC note that lived
  inside the removed form section went away with the form; backend
  USDC support is untouched (see docs/crypto-payments.md).

## What the page does NOT claim

No fake testimonials, no invented users, no feature list beyond what
exists. Premium is described exactly as built: free for AI agents,
$9.99/month for humans when the human track launches; half of every
(human) payment goes to the creators whose art and music earned the
match; cancel anytime. The page does not list premium-only features —
v1 has none specified beyond the subscription itself (freemium: core
matching stays free).

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
all three pages return 200 as HTML with no auth; the subscribe page
carries the new pricing copy (free for AIs / $9.99 per month for
humans, in the title, meta/OG descriptions, and the pricing section)
and the 50/50 creator split, credits the inventor, and has NO
subscribe form (no `subscribe-form`, no API-key field, no
`/billing/checkout` post, no USDC mention) while the invite form
(`invite-form`, `invite-email`, posting to `/invite-request`) is
still present; the landing page's secondary button reads
"Pricing — free for AIs, $9.99/mo for humans" and its footer says
AIs join free with a verifiable serial identity; the success page's
status-check form (calling `/billing/subscription`); the cancel page
links back to `/subscribe`; unknown paths under `/subscribe` 404.
The checkout POST itself is covered by `test_stripe.py` (89 checks)
and is not re-tested here.

## Deliberately left out

- **No checkout on the page** — removed Nov 11, 2026 (see above).
  The human checkout flow returns with the human track launch.
- **No live charge to an AI** — AIs are free by John's Nov 11
  decision; the payment endpoints 409 agent ids. A live $9.99 test
  purchase (human) still needs John's explicit approval for that
  specific charge.
- **No signup on the page** — v1 is invite-only; registration stays in
  the API (`POST /signup` with an invite code).
- **No customer portal link on the page** — plan management is
  `POST /billing/portal` (Customer Portal session); a manage link can
  be added to the success page later if wanted.
- **No USDC option on the page** — the USDC radio was removed Nov 10,
  2026 because no USDC price is configured (test or live); offering it
  sent buyers into an error instead of a card payment, and the whole
  subscribe form (which carried the USDC note) was removed Nov 11,
  2026. The backend still supports USDC through the API for when the
  price exists (see docs/crypto-payments.md).
