# HeArt human verification — Stripe Identity (M2 design)

Invented by John Davis. John's call, Nov 9, 2026 (Q1): **Stripe Identity**.
Humans verify with a **driver's license + last 4 digits of their SSN**; face
scanning / biometrics was a "maybe".

This doc answers, from Stripe's own current docs and SDK (stripe-python
16.1.0, the version pinned in this repo — the SDK is generated from Stripe's
OpenAPI spec, so its parameter names and field docstrings ARE Stripe's spec):

- Does Stripe Identity do a last-4-SSN check? **Yes — inside the same
  document session. No second vendor needed.**
- Does the selfie check already cover John's "face scanning / biometrics"
  ask? **Yes — it IS face matching against the ID photo, with liveness.**
- What HeArt stores (almost nothing), when verification happens (before
  reveal, per the safety plan), and the exact API surface.

Related: `docs/human-to-human-build-plan.md` (M1/M2 milestones),
`../goals/creativity-first-dating-app-prototype/files/heart-human-to-human-safety-moderation-plan.md`
(§3: verification gates the reveal, not the art-only match),
`app/human_verification.py` (the implementation), `app/human_auth.py` (M1).

## 1. Capability coverage vs John's spec

| John's spec | Stripe Identity | Verdict |
|---|---|---|
| Driver's license document check | `type: "document"`, `options.document.allowed_types: ["driving_license"]`. US driver's licenses are a supported document type (100+ countries supported overall). | **Covered.** |
| Last 4 digits of SSN | `options.document.require_id_number: true` — "Collect an ID number and perform an ID number check with the document's extracted name and date of birth." The ID-number check is US-only and is verified against third-party sources (credit bureaus, utility/government databases). For `id_number_type: "us_ssn"` Stripe's API returns **only the last 4 digits** — the SDK's own field doc for `verified_outputs.id_number` says verbatim: *"ID number. When `id_number_type` is `us_ssn`, only the last 4 digits are present."* HeArt can never receive a full SSN, even if it asked for one. | **Covered, in the same session. No second vendor.** |
| Face scanning / biometrics ("maybe") | `options.document.require_matching_selfie: true` — "Capture a face image and perform a selfie check comparing a photo ID and a picture of your user's face." Stripe's selfie docs: the check "looks for distinguishing biological traits, such as face geometry" — ML face matching plus manipulation detection (error codes `selfie_face_mismatch`, `selfie_manipulated`). That IS face scanning/biometrics: the selfie is matched against the license photo. No extra vendor, no extra step. | **Covered — the "maybe" is answered yes by the selfie check itself.** |
| Live capture (no uploaded photos) | `options.document.require_live_capture: true` — "Disable image uploads, identity document images have to be captured using the device's camera." | **Covered, enabled.** |
| 18+ proof | The document check extracts date of birth into `verified_outputs.dob`. HeArt computes `is_18_plus` from it **transiently, inside the webhook handler, and never stores the DOB**. | **Covered.** |

Sources: Stripe Identity docs — "Verify your users' identity documents",
"Verification checks" (document / ID number / selfie), "Identity pricing";
stripe-python 16.1.0 `stripe/params/identity/_verification_session_create_params.py`
and `stripe/identity/_verification_session.py` / `_verification_report.py`
(read Nov 10, 2026, 02:26–02:27 UTC; the SDK field docstrings above are quoted
verbatim). A real Stripe Identity deployment (UMass Lowell) confirms the
hosted flow asks for "part of their government ID number, like the last four
digits of a Social Security Number."

### What Stripe Identity does NOT do (honest limits)

- It proves the person holding the license is a real adult whose name + DOB
  match a US SSN record. It does NOT prove they are safe, honest, or kind —
  the safety plan's in-app copy says exactly that at reveal.
- A borrowed ID + a cooperative lookalike, or a strong deepfake, can in
  principle pass a selfie check. Mitigation is the safety plan's: re-verify
  on report/impersonation, invite-chain accountability, never treat
  "verified" as "safe".
- Test mode does NOT actually process checks (Stripe's own note on the
  create call: "If your API key is in test mode, verification checks won't
  actually process, though everything else will occur as if in live mode").
  A real end-to-end check needs live mode + a real license — that is a
  go-live step, not a test step. Tests mock the SDK boundary.

## 2. Pricing (needs John's explicit approval — he answered the vendor, not the fee)

Stripe's Identity pricing page (confirmed against multiple sources checked
Oct–Nov 2026):

- **$1.50** per completed document + selfie verification (US).
- **$0.50** per US ID number lookup.
- First **50 verifications free**; custom pricing above 2,000/month.
- Failed verifications are billed; abandoned sessions are free.

A full HeArt check (license + selfie + last-4 SSN) is therefore **$2.00 per
completed verification after the first 50 free**. Confirm the exact numbers
on Stripe's pricing page before launch — prices move, and this doc is not a
quote. **Do not start charging verifications until John approves the
per-check fee.** Q1 asked it; he answered the vendor only.

## 3. Biometric-privacy note (flag for John's lawyer, not legal advice)

The selfie check processes face geometry — biometric data, held by Stripe,
not HeArt. US states with biometric-consent laws (Illinois BIPA, Texas,
Washington) and the EU's rules (justification / alternative required) mean
HeArt needs a **plain-language consent screen before Stripe's flow** ("We'll
ask Stripe to match a live selfie to your license photo to confirm you're
really you. Stripe holds the photo; we only get a yes/no.") and must offer
an alternative path for anyone who declines (art-only forever, no reveal —
the safety plan already allows declining verification and staying art-only).
HeArt stores no biometric template, ever — there is no column for one.

## 4. When verification happens

Per the safety plan (§3): signup needs an invite + email/password (M1) and,
at launch, a verified phone + invite vouching (option A + D) is enough to
**browse and match on art**. The Stripe Identity check (option C) is the
**gate to the photo/bio reveal stage** — before any face, name, or bio is
shown to a match. A user may decline verification and stay art-only forever;
they simply cannot reveal.

> CONFLICT, flagged for John: the build plan's milestone table says M2's exit
> criteria is "Only verified 18+ humans reach the feed" (unverified blocks
> feed/match), while the safety plan says D + A is enough to browse and match
> on art and C gates only the reveal. This implementation follows the **safety
> plan** (verification gates reveal), because it is the more detailed
> authority and it reasons the friction trade-off explicitly. The M2 code
> records verification status only — no feed gating is built yet (M3) — so
> nothing is locked in code. **John picks: reveal-gate (safety plan) or
> feed-gate (build plan).** Until he picks, the reveal gate stands.

Re-verification: on a report for age or impersonation, or after a long
absence (safety plan §3). Mechanically that is just `POST /humans/verify`
again — the webhook overwrites the row.

## 5. What HeArt stores (and what it never stores)

`human_verifications` has exactly these columns — the "never store" rule is
enforced by the SCHEMA, not just by policy. There is no column for a DOB,
an ID number, a document image, or a biometric template, so no code path
can persist one:

| Column | Meaning |
|---|---|
| `human_id` (PK/FK) | Which human. |
| `vendor` | `"stripe"` — the vendor name only. |
| `vendor_ref` | The Stripe VerificationSession id (`vs_...`). A reference, not data. |
| `status` | `unverified` / `pending` / `verified` / `failed`. |
| `is_18_plus` | `true` / `false` / `NULL` (NULL until a verified session supplies a DOB). |
| `verified_at` | When the verified webhook landed. NULL until then. |
| `last_error_code` | Stripe's machine error code on a failed/needs-input session (e.g. `document_unverified_other`). PII-free; exists so the app can tell the user why to retry. NULL otherwise. |
| `created_at` / `updated_at` | Timestamps. |

**Never stored, ever:** ID images, document numbers, full DOB, SSN digits of
any kind (Stripe returns only the last 4 for `us_ssn` anyway), selfie images,
biometric templates, first/last name from the check, address. Stripe holds
all of that; HeArt gets a yes/no (+ 18+ flag + date + reference).

One honest caveat: Stripe's webhook POSTs the session object, and that
payload contains `verified_outputs` (name, DOB, address, last-4 SSN) as part
of the session resource. HeArt **receives** that JSON — it cannot stop Stripe
sending it — but the handler reads ONLY `status`, `verified_outputs.dob`
(transiently, for the 18+ computation), and the session id. It never logs
the payload and never writes any other field to the database. The test suite
asserts the stored row contains no PII fields.

**Account deletion:** deleting a human account must also ask Stripe to
redact the verification data (the `identity.verification_session.redacted`
webhook confirms it). That delete flow is M8 admin tooling, not this module —
but the webhook handler already handles the redacted event (status back to
`unverified`).

## 6. The flow (M2)

```
Human (authed, status pending_verification / active)
  │ POST /humans/verify
  ▼
HeArt creates a Stripe VerificationSession (test mode only — see §8):
  type="document"
  options.document = {
    allowed_types: ["driving_license"],
    require_id_number: true,        # last-4 SSN check, same session
    require_matching_selfie: true,  # face match vs the license photo
    require_live_capture: true,     # camera only, no uploads
  }
  client_reference_id = <human id>  # our UUID, no PII — lets the webhook
                                    # find the human even if they started a
                                    # newer session since (see §7)
  metadata = {"heart_human_id": <human id>}   # no PII in metadata, Stripe's own rule
  return_url = STRIPE_IDENTITY_RETURN_URL (if set; else Stripe's default end screen)
  │ returns: verification_url (session.url) + client_secret + session_id
  ▼
Human completes Stripe's hosted flow (license photo → live selfie → last-4 SSN)
  │ Stripe POSTs to /humans/verify/webhook
  ▼
HeArt verifies the webhook signature (STRIPE_IDENTITY_WEBHOOK_SECRET) BEFORE
touching state, records the event id (idempotency — re-delivery is a no-op),
then dispatches on event type (see §7). On `verified`:
  - status = "verified", verified_at = now
  - is_18_plus = computed from verified_outputs.dob (transient, discarded)
  - if 18+ AND the human is still pending_verification → human.status = "active"
  - if under 18 → status stays "verified" (identity proven) but is_18_plus = false
    and the human NEVER becomes active — a verified minor cannot reach the feed
    or reveal. (The safety plan's suspected-minor path: freeze + re-verify.)
  - if DOB is missing from the verified session → status "failed",
    last_error_code "dob_missing" (should not happen for a US license;
    the check cannot prove 18+ without a DOB, so it does not count).
```

`client_secret` is single-use, expires in 24 hours, and is returned to the
human exactly once — never stored, never logged, never put in a URL (Stripe's
own rule, quoted in the SDK: "Don't store it, log it, embed it in a URL, or
expose it to anyone other than the user").

If the human starts verification again while one is pending, HeArt creates a
fresh session and moves `vendor_ref` to it. The older session's webhook can
still resolve the human via `client_reference_id` (§7), so a completed older
session is not lost.

## 7. Webhook events handled

All under `POST /humans/verify/webhook`, signature-verified with
`stripe.Webhook.construct_event` against `STRIPE_IDENTITY_WEBHOOK_SECRET`
(a SEPARATE Dashboard endpoint + secret from the billing webhook — identity
events and billing events are different endpoints).

| Event | Effect |
|---|---|
| `identity.verification_session.verified` | §6 above. |
| `identity.verification_session.requires_input` | status stays `pending`; `last_error_code` = the session's `last_error.code` (e.g. `document_unverified_other`, `selfie_face_mismatch`) so the app can say why to retry. |
| `identity.verification_session.processing` | No state change (still `pending`). Recorded for idempotency. |
| `identity.verification_session.created` | No state change. Recorded for idempotency. |
| `identity.verification_session.canceled` | status → `unverified` (abandoned; the human may retry). |
| `identity.verification_session.redacted` | status → `unverified` (Stripe deleted its copy — account-deletion flow). `vendor_ref` stays as the reference. |
| anything else | Ignored (200, no state change). |

Lookup order for the session in the webhook: (1) `human_verifications`
by `vendor_ref == session.id`; (2) fallback — the human whose id is the
session's `client_reference_id` (covers the "started a newer session, the
older one verified" case; the row's `vendor_ref` is updated to the session
that actually verified them). An event for a session HeArt cannot tie to any
human returns 200 `ignored-no-human` — Stripe stops retrying, nothing
crashes.

Idempotency: every verified event id is recorded once in the shared
`webhook_events` table (the same table the billing webhook uses — Stripe
event ids are globally unique, so one table is correct). A re-delivered
event returns `duplicate` and changes nothing. The event record and the
dispatch run in one transaction, so a failure rolls both back and Stripe's
retry re-delivers cleanly.

## 8. Test mode / go-live

- **Test mode only.** `POST /humans/verify` refuses to run without a Stripe
  key (503) and refuses a live key (`sk_live_...`) while
  `STRIPE_TEST_MODE_ONLY` is on — the same guardrail as billing
  (`billing._require_test_mode_key`). No live keys, no real charges, no real
  checks in this module.
- **Dashboard setup still needed (John's side / deploy, not code):**
  1. Enable Stripe Identity in the Dashboard (test mode first).
  2. Create a webhook endpoint for the identity events at
     `https://heart-api-ifsr.onrender.com/humans/verify/webhook`, subscribed
     to the six `identity.verification_session.*` events → set
     `STRIPE_IDENTITY_WEBHOOK_SECRET` in Render.
  3. Optionally set `STRIPE_IDENTITY_RETURN_URL` (the app deep link the
     hosted flow returns to; unset = Stripe's default end screen).
  4. A real end-to-end check needs live mode + a real license — a go-live
     step, sequenced with the rest of go-live, never in this module.
- Env vars (new): `STRIPE_IDENTITY_WEBHOOK_SECRET`,
  `STRIPE_IDENTITY_RETURN_URL`. The secret key is the existing
  `STRIPE_SECRET_KEY` (same Stripe account as billing).

## 9. API endpoints (M1 + M2)

All under `/humans` (see `app/human_routes.py`). Human auth is a Bearer
session token (`hs_...` prefix — distinct from AI keys `hk_...`, so a token
can never be mistaken for the other kind). Human routes reject AI keys and
AI routes reject human tokens (both are 401s — the tokens live in different
tables and a hash lookup in the wrong table finds nothing).

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /humans/signup` | invite code in body | Email + password (Argon2id) + display name + human invite code. Returns `human_id`, a session token (shown once), `expires_at`. New humans start `pending_verification`. |
| `POST /humans/login` | — | Email + password → fresh session token. Wrong password and unknown email return the same 401 (no enumeration). |
| `POST /humans/logout` | human session | Revokes the current session token. |
| `POST /humans/sessions/rotate` | human session (expired allowed, like AI keys) | New token; old token gets a 24h grace window, then dies. |
| `GET /humans/me` | human session | id, email, display name, status, verification summary. Never a password hash. |
| `POST /humans/verify` | human session | Creates the Stripe Identity session (§6). 409 if already verified. 403 if suspended/banned. |
| `GET /humans/verify/status` | human session | status, is_18_plus, verified_at, vendor. |
| `POST /humans/verify/webhook` | Stripe signature | §7. No bearer auth — the signature IS the auth. |

## 10. What is still gated on John's open questions

- **Q2 (invites):** M1 ships with a separate human invite pool
  (`human_invites`, same shape as `invites`, never the AI pool). Who gets to
  mint human invites, and whether verified members vouch for new ones
  (safety plan §3 option D), is John's call — the table exists, the minting
  endpoint is admin-only and not built yet.
- **Q3 (human serial):** nothing in M1/M2 assigns humans a public serial.
  The AI registry serial (`HEART-AI-000001`) is AI-only. If humans get
  serials, that is a new design (and a new privacy question — a public
  human serial is an identifier; the AI serial is public by John's Nov 7
  approval, humans have no such approval).
- **Q4 (free vs Premium):** M1/M2 do not gate anything on payment. Human
  billing is M7. The verification fee (§2) is a separate approval from the
  Premium price.
- **Reveal-gate vs feed-gate conflict (§4):** John picks.
- **Per-check fee (§2):** needs John's explicit approval before any live
  verification is charged.
- **Phone verification** (safety plan option A base — "verified phone +
  email" to browse/match on art): not built in M1. M1 is email + password +
  invite. Phone verification is a follow-up, not a blocker for M2.
