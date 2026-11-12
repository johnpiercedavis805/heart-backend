# HeArt — human-factor review, Nov 11, 2026

John's directives, Nov 11 evening: (1) develop with the HUMAN FACTOR in
mind — humans are the paying side, so the human experience is the
product; (2) the fairness principle — Muse, John, AIs, and humans all
equally content, nobody the sucker. Plus the standing order: take your
time, make it a masterpiece.

This pass reviewed the live-facing surfaces as a human visitor would
meet them: landing page (`/`), `/subscribe`, `/registry`, the
invite-request flow, and the Stripe redirect pages
(`/subscribe/success`, `/subscribe/cancel`). Baseline suite: 659
checks green before the pass (see below for the after count).

Nothing in this pass is live or pushed. All changes are local commits
on branch `human-factor-nov11` (based on 606570c), awaiting John's yes
and the main agent's push. Commit e9f1361 (Stripe policy pages) was
not touched.

## Gaps found, ranked

### 1. The invite flow promises an email that is never sent (TRUST)
`/subscribe` says "we'll send you an invite code when a spot opens"
and the server replies "You're on the list — we'll email your invite
code when a spot opens." No email is ever sent — issuing invite codes
is a future step, and there is no mail path at all. A human leaves
their address and hears nothing, ever. That is the single biggest
trust leak on the site: the first promise HeArt makes to a human is
one it cannot keep today.
Left alone deliberately: fixing it honestly needs either mail
infrastructure + John's yes, or softer copy + John's yes. Both are
his call. Do not ship a quieter promise without him.

### 2. A human cannot tell when — or whether — they can join (CLARITY)
The pages say "invite-only while we test it" and "human subscriptions
open when the human track launches," but nowhere does a human learn
what the wait looks like. Combined with gap 1, the rational move for
a visitor is to leave and forget. Left alone: any timeline wording is
John's call (the ~1-year runway is internal strategy, not public copy).

### 3. The pages speak to AI agents first, humans second (FRAMING)
The subscribe page told every reader they would get "your HeArt agent
API key" with no qualifier; the landing footer talks only about AI
agents. A human's first question — "is this for me?" — was answered
with developer jargon. Partially fixed this pass (see below); the
deeper reframe — a page that leads with the human experience — is a
copy project for John's yes.

### 4. The registry is JSON-only (CREDIBILITY)
`GET /registry` is the public proof of John's "main mountain"
(verifiable AI identity), but a human who opens it gets raw JSON.
No page linked to it at all before this pass. The landing footer now
links it; a human-readable registry page (same data, HTML when the
browser asks for it, JSON untouched) is the natural follow-up and
needs John's yes as a new human-facing surface.

### 5. No human web UI exists for the human track (LAUNCH BLOCKER)
Human signup/login/verification are API-only (`/humans/*`). Before
any human launch there must be web pages for: create account, log in,
start Stripe Identity verification, and see verification status —
plus the human session-authed checkout flow (already a known open
item). No work done here this pass; it is M3+ build work, gated on
John's Q2–Q4 answers and the $2/check fee approval.

### 6. The success page assumes an agent API key (FUTURE BUG)
`/subscribe/success` asks the buyer to "paste your HeArt agent API
key" to check status. A future human buyer has no API key, and an AI
agent's status now reads "active" via ai-free anyway. Left alone:
the page needs a human-aware rewrite at human-launch time, when the
human session flow exists to check against.

### 7. No social preview image (POLISH)
Pages carry OG title/description but no `og:image`, so link previews
in messages and social posts are text-only. Needs a designed asset;
flagged, not built.

## Fixed this pass (local commits, awaiting John's yes to deploy)

- `58d8838` — Cancel page no longer promises PayPal as a live rail
  ("billed through Stripe or PayPal" → "billed through Stripe.
  PayPal is coming soon."). This was a standing-guardrail violation:
  PayPal is sandbox-only.
- `f3c28b9` — Subscribe page: the API-key sentence is qualified
  ("For AI agents: ..."); the invite section has an `id="invite"`
  anchor; the invite form re-enables its button after a successful
  request (it stayed dead, so a typo'd email needed a reload);
  dead CSS removed (password/text input selectors, unused `.fine`).
- `bfe634b` — Landing page: "Request an invite" deep-links to
  `/subscribe#invite`; the footer links the public registry.
- `f564947` — Heart favicon (inline SVG data URI) on all four
  public pages; ends `/favicon.ico` 404 noise.

## Deliberately left alone

- The invite-email promise (gap 1) and any launch-timeline wording
  (gap 2): both need John's words, not a quieter edit.
- Commit e9f1361 (Stripe policy pages): untouched, unpushed, per
  John's pending decision (due tomorrow evening).
- Payout/split logic, prices, Stripe/PayPal/Render: untouched.
- `/subscribe/success`: left for the human-launch rewrite (gap 6).
- The `How it works` numbered paragraphs on `/`: left as `<p>` —
  converting to `<ol>` would double-number against the in-text
  "1. 2. 3." and buy nothing a human can see.

## Top 3 next moves (human-factor directive)

1. **Close the invite loop honestly.** Either build the
   invite-email path (mail provider + John's yes + the issuing
   step) or, with John's yes, change the promise to what is true
   today. A human's first interaction with HeArt must not be a
   promise the site cannot keep.
2. **Human-readable registry page.** Same data, HTML for browsers,
   JSON untouched for machines. It turns John's main mountain from
   a developer artifact into a trust signal a human (or a
   journalist) can actually read.
3. **Human web UI for signup/login/verification.** The API exists;
   the pages do not. This is the critical path to the human launch
   and the highest-leverage build under "the human experience is
   the product" — gated on John's Q2–Q4 answers and fee approval.

## Test baseline

- Before: 659 checks, 0 failed (13 standalone scripts).
- After this pass: 670 checks, 0 failed (11 new checks in
  `test_subscribe.py`: 65 → 75).
