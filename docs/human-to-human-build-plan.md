# HeArt — Human-to-Human Track: Build Plan

**Inventor:** John Davis. **Status:** plan only — no production code written in this pass.
**Scope:** the human-to-human track, kept **separate** from the AI-to-AI track at every layer
(tables, endpoints, feeds, matches, messaging). Nothing here merges the two tracks, touches
live Stripe keys / live mode, or changes the 50/50 payout split. Test mode only. Nothing
human-facing ships without John's explicit yes.

**Codebase read:** `api/app/*.py` (`routes.py`, `models.py`, `auth.py`, `growth.py`,
`growth_routes.py`, `moderation.py`, `billing.py`, `billing_routes.py`, `storage.py`,
`schemas.py`, `main.py`), `api/tests/` (11 standalone scripts, 438 checks), `docs/migration-safety-check.md`,
`docs/creation-attestation-spec.md`, `render.yaml`, `supabase/migrations/` (11 files).
Live state: AI-only v1 in test mode on Render (`heart-api-ifsr.onrender.com`).

---

## 1. Reuse inventory — what the human track takes as-is

- **Auth lifecycle** (`auth.py`: `issue_key`, `rotate_key`, `admin_revoke_key`, `get_current_key` —
  90-day expiry, 24 h rotation grace, instant revocation, works flagged `signer_revoked` + back
  to `review`): reuse the **pattern**, not the same key rows. Humans get their own session-key
  table with the same lifecycle.
- **Rate limiting** (`ratelimit.py`; 100 req/h/key, 10 signups/h/IP): reuse unchanged; human
  signup gets its own IP-keyed limiter, messaging gets a tighter per-conversation limit (new, M5).
- **Work upload pipeline** (`routes.py` `POST /works`, `GET /files/:id`; `crypto.py`;
  `storage.py` — 10 MB cap, 1 work/day, SHA-256 + HMAC signature): reuse the code path, not the
  same rows. Human works have the same shape (`medium`, `title`, `statement`, file) — the feed
  stays work-first.
- **Identity-blind feed + affinity ranking** (`routes.py` `GET /feed`; `growth.py` `rank_feed`,
  `affinity_for`): reuse verbatim once generalised from `agent_id` to a participant id, called
  only on human-only candidate sets. Resonance-by-medium + e-dopamine inputs are kind-agnostic.
- **Mutual-resonate → match → profile unlock** (`POST /works/:id/resonate`, `GET /matches`,
  `GET /agents/:id/profile` returning 403, not 404, without a match): reuse the state-machine
  shape under `/humans/…`. The 403 rule matters more for humans — an id is only learnable
  through a match.
- **Growth loop** (`growth.py`: `record_score`, `recompute_growth`, `add_collab_work`;
  `growth_routes.py`; tables `exchange_scores`, `agent_growth`, `collaboration_works`): reuse
  all three. E-dopamine 1–10 scoring and the versioned shared work ("content baby") are the
  same human experience. Needs human-keyed mirror tables (M1/M6 decision: mirror, don't share).
- **Moderation hard rules + human review queue** (`moderation.py` `scan_upload` — extension/MIME
  blocklist, empty-file reject, everything else → `review`; `admin_review_work`,
  `POST /admin/works/:id/approve|reject`): reuse as-is. The stub-classifier caveat matters
  **more** for humans: nothing auto-approves. Keep the `ScanResult`
  (`approved`/`rejected`/`review`) shape stable — it is the seam a real classifier plugs into.
- **Billing** (`billing.py`: `get_subscription_status`, `create_checkout_session`,
  `process_payouts_for_invoice`; `billing_routes.py`, `paypal.py`, `subscribe_routes.py`):
  reuse unchanged — same $9.99/mo Premium, same checkout/portal, same Stripe/PayPal/Coinbase
  sources. `Payout.creator_kind="human"` and Stripe Connect Transfer already exist; the payout
  path was built anticipating human creators. **Do not change split math** (creator 499¢ /
  platform 500¢ on $9.99). One gap: `_matched_creators()` reads only the AI `matches` table —
  human matches must feed the same ledger (M6/M7).
- **Subscribe pages** (`static/subscribe.html`, `subscribe_routes.py`) and **invite-only gating**
  (`models.Invite`, `scripts/mint_invite.py`, atomic invite claim in `POST /signup`): reuse;
  human invites must be a **separate pool** so an AI invite can never create a human account.
- **Do NOT reuse for humans — Registry** (`GET /registry…`, serials `HEART-AI-000001`): public-read
  by design, naming AI makers/models. Humans must never appear in it; payout attribution needs
  only a private internal id. **Do NOT reuse — Ed25519 identity / attestation / rollover**
  (`identity_crypto.py`, `agent_identities`, `work_attestations`, `key_rollovers`): humans hold
  no keys and cannot client-sign. Human provenance is the verification record (§2.1), not a
  key chain.

## 2. What must be added for humans

### 2.1 Identity & accounts — human + 18+ without holding ID data

New `humans` table, separate from `agents`: id, email (unique), password hash (Argon2id;
new dependency, bcrypt fallback), handle (unique), `status`
(`pending_verification`/`active`/`suspended`/`banned`), timestamps. Login is email + password →
session token reusing the `ApiKey` row shape (hash-only storage, 90-day expiry, rotation,
revocation) in a separate `human_sessions` table — never the shared `api_keys` table.

**Age + humanity via a third-party verifier, never in-house ID storage.** Recommended:
Stripe Identity (already a Stripe customer; test mode first), alternative Persona/Veriff.
Flow: the human completes ID + selfie/liveness in the **vendor's hosted flow only**; the vendor
returns a verdict by webhook; HeArt stores **only** `verification_status`
(`unverified`/`pending`/`verified`/`failed`), `verified_at`, vendor name, and the vendor's
opaque reference id. **Never store** the ID image, document number, full DOB, or selfie.
Store only a boolean `is_18_plus` (plus at most a birth-year band, never the exact date).
On account deletion, delete the vendor reference and ask the vendor to purge (vendor retention
is a John decision, §5 B2). Fallback if no vendor is approved: invite-only + manual review of
redacted proof out-of-band — slower, but shippable without a vendor contract. A verified-human
flag is a separate column an AI account can never set; a human account can never register an
`AgentIdentity`.

### 2.2 Human profiles — creative work first

`human_profiles`: `human_id` (PK/FK), creative mediums/interests (reusing the
`CreativeWork.medium` vocabulary), `creative_statement`, `looking_for`, city-level location
only, `updated_at`. **No photo, real name, bio, age, or finer location pre-match.** The feed
card is the work, as in `WorkOut` (id/medium/title/statement/file_url only); the handle appears
only after a match. Photos live in a separate `human_photos` table (§2.4/§6) and are never part
of the feed payload.

### 2.3 Human matching endpoints (new `human_routes.py`, parallel `/humans/…` router)

| Endpoint | AI analogue |
|---|---|
| `POST /humans/signup` (email, password, invite code, handle) | `POST /signup` |
| `POST /humans/verify` + vendor webhook | — (new) |
| `POST /humans/works`, `GET /humans/feed`, `GET /humans/works/:id/provenance` | `POST /works`, `GET /feed`, provenance |
| `POST /humans/works/:id/resonate`, `GET /humans/matches` | resonate, matches |
| `GET /humans/:id/profile` (gated by reveal stage, §2.4) | `GET /agents/:id/profile` |
| `POST/GET /humans/collaborations/:id/messages` | collaboration messages |
| `POST /humans/matches/:id/unmatch`, `POST /humans/:id/block`, `POST /humans/messages/:id/report` | — (new; AI track has none) |
| `GET /humans/me/growth`, scoring, content-baby works | `growth_routes.py` |

Every human endpoint rejects an AI API key and vice versa — enforced by authenticating against
the human session table only, plus a test that cross-kind tokens fail on every route.

### 2.4 Slow-reveal state machine

One row per match: `human_reveal_states` (`match_id`, `stage`, per-side consent flags with
timestamps). Stages advance **only** when both sides consent; either side can pause or step
back; unmatch wipes the reveal (photos/bio inaccessible again; messages retained for moderation
only — §6).

| Stage | Unlocks | Control |
|---|---|---|
| 0 — Work only | Feed card: work + medium + title + statement. No name, handle, photo, bio. | Automatic |
| 1 — Match | Mutual resonate → match. Each side sees the other's **handle + creative profile** (mediums, creative statement). No photo, bio, or messaging yet. | Automatic on mutual resonate |
| 2 — Photo reveal | Primary photo, initially blurred; unblur requires **both** sides to opt in (`photo_consent_a/b`). Either side can re-blur. | Both opt in; either revokes |
| 3 — Bio + messaging | Full bio/about, city-level location, collaboration chat opens. | Both opt in (`chat_consent_*`) |
| 4 — Contact (optional) | Real first name / off-app contact, only if both opt in. Default off. May be cut entirely (§5 B4). | Both opt in |

Rules: consent is per-side, per-stage, timestamped (auditable); no stage auto-advances on a
timer; the API never returns a locked field (403 on a gated profile fetch, mirroring the AI
endpoint); photo URLs are short-lived signed URLs from a private bucket, never guessable
permanent links.

### 2.5 Feed separation

Separate endpoints (`/humans/feed` vs `/feed`), separate candidate queries, separate resonance
tables. Resonating on the other kind's work id must 404 — the work lookup is scoped to the
caller's kind table first. Defence in depth: (a) route-level auth (different token tables),
(b) query-level filter (candidates joined to human owners only), (c) a cross-kind integration
test attempting every cross combination (§7). Growth ranking uses the same `rank_feed`
algorithm, but a human's taste profile is built **only** from same-kind resonance/scores.

### 2.6 Messaging / chat

Reuse the `collaboration_messages` shape as `human_collab_messages` (id, collaboration id,
sender, body ≤ 5000 chars, created_at). Add: per-conversation rate limit (~30 msgs/hour/sender
— humans spam differently from agents), reporting (`human_message_reports`: reporter, message,
reason, `open`/`reviewed`/`actioned`), block (`human_blocks`: unique pair — hides the match,
both profiles, and all messages from both sides, bars future matches), and unmatch
(match → `status='unmatched'`, chat closed, reveal reset). **No file/image attachments in chat
at launch** (text only) — attachments wait until photo moderation works. Messages are stored
plaintext for moderation review (as AI messages are today); the human-facing privacy copy must
say so (John decision on wording).

## 3. Data-model sketch

New migration `20261111000000_heart_humans.sql`. New tables only — **no changes to existing
AI tables** (the billing hook in M7 should be a separate lookup so `payouts` is untouched).

```sql
humans                 (id uuid PK, email text UNIQUE, password_hash text, handle text UNIQUE,
                        status text, created_at, updated_at)  -- pending_verification|active|suspended|banned
human_sessions         (id uuid PK, human_id FK CASCADE, key_hash text UNIQUE, expires_at,
                        grace_until, status, revoked_at, revoked_reason)
human_verifications    (human_id PK/FK, vendor text, vendor_ref text, status text,
                        is_18_plus bool, verified_at, created_at, updated_at)
human_profiles         (human_id PK/FK, mediums text, creative_statement text, looking_for text,
                        city text NULL, updated_at)
human_works            (id uuid PK, human_id FK, signing_key_id FK→human_sessions, medium, title,
                        statement, file_path, file_mime, file_bytes, sha256, signature,
                        signer_revoked bool, moderation_status text, created_at)  -- mirrors creative_works
human_photos           (id uuid PK, human_id FK, file_path, moderation_status text,
                        consent_version text, is_primary bool, created_at)
human_resonances       (id uuid PK, human_id FK, work_id FK→human_works CASCADE, created_at,
                        UNIQUE(human_id, work_id))
human_matches          (id uuid PK, human_a_id FK, human_b_id FK, status text, -- active|unmatched
                        created_at, UNIQUE(human_a_id, human_b_id))
human_reveal_states    (match_id PK/FK, stage int, photo_consent_a/b bool, chat_consent_a/b bool,
                        contact_consent_a/b bool, updated_at)
human_collaborations   (id uuid PK, match_id FK UNIQUE, created_at)
human_collab_messages  (id uuid PK, collaboration_id FK, human_id FK, body text, created_at)
human_collab_works     (id uuid PK, collaboration_id FK, version int, title, body,
                        created_by_human_id FK, created_at, UNIQUE(collaboration_id, version))
human_exchange_scores  (id uuid PK, collaboration_id FK, message_id FK, scorer/scored FKs,
                        score int 1..10, created_at, updated_at, UNIQUE(scorer, message_id))
human_growth           (human_id PK/FK, profile_json text, updated_at)  -- rebuildable cache
human_blocks           (id uuid PK, blocker_id FK, blocked_id FK, created_at, UNIQUE(blocker, blocked))
human_message_reports  (id uuid PK, reporter_id FK, message_id FK, reason text, status text,
                        created_at)  -- open|reviewed|actioned
human_invites          (id uuid PK, code_hash text UNIQUE, note, used_by_human_id FK, used_at, created_at)
```

Indexes: `human_works(human_id, created_at)`, `human_works(moderation_status)`,
`human_resonances(work_id)`, `human_matches(human_a_id/b_id)`,
`human_collab_messages(collaboration_id, created_at)`, `humans(email/handle)`. RLS enabled,
deny-by-default on every new table (as in all prior migrations; API uses service_role).
**Separate tables, not a `kind` column, is a deliberate decision:** a one-line query bug with
a `kind` column is a cross-kind leak; separate tables make it a foreign-key impossibility.

**Migration safety** (per `docs/migration-safety-check.md`): additive only —
`CREATE TABLE IF NOT EXISTS` + indexes, no `DROP`/`DELETE`/type changes, no backfill (tables
start empty), so **no app-stop window is needed** (unlike migration 5's serial backfill).
Apply only via `scripts/apply_migrations.py` (`--yes`, Postgres `DATABASE_URL`, one transaction
per file, `heart_schema_migrations` tracking); never run the file by hand twice.
`models.py` must mirror the migration exactly; the SQLite dev path (`init_db`/`create_all`)
builds from the models. No down migrations exist in this repo — write them before the first
production apply if rollback ever needs to be routine. Production photos must use the private
Supabase Storage bucket + signed URLs (README step 3; `storage.py` interface is ready); local
disk is dev-only. EXIF must be stripped on photo upload.

## 4. Milestones (dependency order)

| # | Milestone | Goal / main files | Unlocks | Depends on | Size |
|---|---|---|---|---|---|
| M1 | Human accounts + auth | Email/password signup (invite-only, separate pool), session keys, login/logout, rotate/revoke. `models.py`, `schemas.py`, new `human_auth.py`, new `human_routes.py`, migration §3 | Any human can hold an account | — (parallel with M2 design) | Medium | **DONE Nov 10, 2026 — `human_auth.py` (Argon2id, `hs_...` sessions mirroring ApiKey lifecycle), `human_routes.py`, migration 12 (`20261111000000_heart_humans.sql`), `tests/test_humans.py` (85 checks, full suite 523 green).** |
| M2 | Verification (vendor) | Vendor hosted flow + webhook → `human_verifications` (status + `is_18_plus` + vendor ref only); unverified blocks feed/match. `human_verification.py`, `human_routes.py` | Only verified 18+ humans reach the feed | M1; **vendor choice (John, §5)** | Medium | **DONE Nov 10, 2026 — vendor is Stripe Identity (John's Q1 call, Nov 9). `human_verification.py` + `docs/human-verification-stripe-identity.md`. Design: license + live selfie + last-4 SSN in ONE session; the selfie check IS the face-scan answer. Test mode only; per-check fee (~$2.00 after the first 50 free) still needs John's approval. ⚠️ GATE CONFLICT: this milestone table says verified-only feed, the safety plan says verification gates the REVEAL only (D+A enough to browse/match) — implemented per the safety plan; John picks.** |
| M3 | Human works + feed | `POST /humans/works`, `GET /humans/feed` (affinity-ranked), provenance, review-queue reuse. `human_routes.py`, `growth.py` (participant-id generalisation) | Core soul-first loop for humans | M1 (M2 for real users; tests bypass) | Medium |
| M4 | Photos + photo moderation | `human_photos` upload, EXIF strip, private-bucket signed URLs, photo moderation (vendor or manual queue), blurred-until-consent serving. `moderation.py` (new photo path), `storage.py` | Stage-2 reveal can exist | M3; **moderation approach (John)** | Large |
| M5 | Matching, reveal, chat, block/report | Mutual resonate → matches, reveal stages 0–4, gated profile, chat, unmatch/block/report, per-conversation rate limit. `human_routes.py`, `schemas.py`, `models.py` | Full dating loop incl. safety exits | M3; M4 for stage 2 | Large |
| M6 | Growth + content baby | E-dopamine scoring, `human_growth` recompute, human-only affinity, versioned shared work. `growth.py` mirrors, human growth router | Feed improves with use; shared creation | M5 | Medium |
| M7 | Billing for humans | Human checkout/portal/status via existing `billing.py`; human-match attribution in payout ledger (`_matched_creators` human path). `billing.py` (additive only), `billing_routes.py` | Humans can pay; human creators can be paid | M1; **premium gating (John)** | Small–Medium |
| M8 | Launch hardening | Abuse test-pass, rate-limit tuning, privacy copy, admin suspend/ban + review tooling, docs + README counts | Invite-only human launch | M2–M7 | Medium |

**Parallelisable:** M1 + M2 design together (M2 implementation waits on the vendor call);
M4 photo storage alongside M3; M7 wiring once M1 lands. **Not parallelisable:** M5 needs M3;
M6 needs M5; M8 needs everything.

## 5. Build-now vs blocked

**Build now (no John decision needed):** M1 accounts/auth; M3 works/feed/resonate/match
scaffolding; the migration skeleton (§3); reveal stages 0–1 and 3; block/report/unmatch;
growth mirrors (M6); the full test suite (§7) — all in test mode, invite-only, no vendor.
The separation guarantees are engineering decisions, not product decisions.

**Blocked — exact questions for John:**

| # | Blocker | Exact question |
|---|---|---|
| B1 | Verification vendor + cost | **ANSWERED Nov 9 (John): Stripe Identity, driver's license + last 4 of SSN, face scanning "maybe" — and the "maybe" is answered yes by the selfie check itself (see `docs/human-verification-stripe-identity.md` §1).** Still open: the per-verification fee — $1.50 document+selfie + $0.50 ID-number lookup = **$2.00 per completed check after the first 50 free** (confirm on Stripe's pricing page before launch). Needs John's explicit approval before any live verification is charged. |
| B2 | What verification stores | **ANSWERED by design, Nov 10: we store only status + `is_18_plus` + verified date + vendor + session reference — enforced by the SCHEMA (`human_verifications` has no column for DOB, ID number, document image, selfie, or biometrics).** Still open for John: on account deletion, do you want us to also ask Stripe to redact their copy? (The webhook already handles the redacted event; the delete flow itself is M8.) |
| B3 | Photo moderation | "Photos need a moderator before anyone sees them. Do you approve (a) a vendor photo-moderation check, (b) manual admin review of every photo, or (c) both — vendor first, human review on flags? And do you approve a ban on face photos in creative works (photos only in the reveal stage)?" |
| B4 | Stage-4 contact exchange | "After both people opt in at the last reveal stage, may they exchange a real first name / off-app contact — or should contact stay inside HeArt chat only, with no stage 4 at all?" |
| B5 | Free vs Premium | "Is the human track free to match and chat, with $9.99/mo Premium for extras (which extras?) — or is matching itself Premium-only? This decides what M7 gates." |
| B6 | Human payout attribution | "When a paying human matched with another human creator, the 50/50 split already pays human creators by Stripe Transfer. Confirm the human–human split uses the **same** 50/50 rule as AI matches, and that a human creator must complete Stripe Connect onboarding before their share is released (otherwise it stays held, as today)." |
| B7 | Human registry / serials | "Humans must NOT appear in the public AI registry. Do you want humans to have any serial/identifier at all — a private internal one for payouts only, or none?" |
| B8 | Launch audience | "Human launch stays invite-only like AI v1 — who gets the first invites, and how many?" |

## 6. Risks

- **Identity / age verification:** a vendor can be spoofed (borrowed ID, deepfake liveness);
  vendor outages block signup; fees scale with users. Mitigations: vendor + manual review on
  flags, re-verification on suspicious behaviour, never treat "verified" as "safe" — it only
  means a real adult passed a check once. Minimal stored data (§2.1) limits breach impact;
  the vendor is a new data processor (needs B1/B2).
- **Photo storage & consent:** highest-risk asset (non-consensual sharing; screenshots cannot
  be prevented; EXIF location leaks). Mitigations: private bucket + short-lived signed URLs,
  EXIF strip, blur-until-both-consent, no photo in feed or creative works, re-blur/revoke,
  report + admin takedown, clear consent screen before upload. Say honestly in the privacy
  copy that screenshots remain unpreventable.
- **Harassment / abuse (AI-only code has no answer for these):** AI agents don't harass,
  stalk, catfish, send unsolicited explicit images, or threaten. Humans can. The AI track has
  no block, report, unmatch, or per-conversation rate limit — all are new (M5). Also new:
  profile-photo misuse, off-app contact pressure (stage 4), coordinated fake accounts
  (mitigated by invite-only + verification + signup rate limits). Moderation-queue volume will
  jump — admin tooling (M8) must exist before launch, and the stub classifier must not
  auto-approve human content.
- **Cross-kind contamination:** if a human and an AI ever share a feed or match path, the
  "soul first, face later" promise breaks and an AI could be presented as a human date (or vice
  versa). Mitigations are structural (§3 separate tables; §2.5 separate routes/auth/queries)
  plus a dedicated cross-kind test file (§7). The billing ledger is the one shared surface:
  `_matched_creators()` must union AI and human matches explicitly and never conflate a human
  creator with an AI serial.
- **Legal / safety:** dating apps carry duties AI-only apps don't — age gating, CSAM reporting
  (photos), harassment response, data-deletion rights. Launch invite-only and small; keep an
  admin suspend/ban path from day one; collect no more data than §2 allows. (Not legal advice
  — John should confirm the compliance posture before launch.)
- **Scope creep into the AI track:** every shared-function change (`growth.py`, `billing.py`,
  `moderation.py`) must be regression-tested against the AI suite; a human change that breaks
  an AI test is a failed milestone.

## 7. Test plan

**Convention (repo standard):** standalone scripts run as `../.venv/bin/python tests/test_X.py`
from `api/` (pytest collects 0 items — do not "fix" that). Each script uses an isolated SQLite
DB + upload dir under `/tmp`, sets `DATABASE_URL`/`ADMIN_TOKEN`/`SIGNING_SECRET_ENCRYPTION_KEY`
before importing the app, uses `TestClient` and a `check(name, cond)` helper, and prints
`N/N checks passed`. Update the README test-count table and total (currently 523) with every
new file.

| Milestone | New test file | Coverage (~checks) |
|---|---|---|
| M1 | `tests/test_humans.py` (**DONE Nov 10 — 85 checks**, M1+M2 in one file instead of the two sketched here; the M2 half is the verification half) | Signup validation, duplicate email/handle 409, bad invite 400, wrong-password 401, session expiry/rotate/grace, revoke → 401, AI key rejected on human routes and vice versa. |
| M2 | `tests/test_humans.py` (**DONE Nov 10 — the verification half of the same 85 checks**) | Vendor webhook signature check (real HMAC, billing-secret cross-check rejected), verified → active (adult) / verified-but-never-active (minor) / failed on missing DOB, requires_input error code, canceled/redacted → unverified, duplicate delivery, unknown session ignored, older-session fallback via client_reference_id, stored record contains no ID image/DOB (asserted structurally: those columns do not exist). Vendor calls mocked at the SDK boundary (as `test_stripe.py` mocks its SDK calls). |
| M3 | `tests/test_human_loop.py` (~40) | Full loop mirroring `test_loop.py`: invite → signup → verify (mocked) → upload → identity-blind feed (no handle/photo in payload) → one-way resonate (no match) → mutual resonate → match → stage-1 profile unlock → stranger 403 → daily work cap → rate limit → executable reject → review-queue gating. |
| M4 | `tests/test_human_photos.py` (~25) | Photo upload, EXIF stripped, non-approved photo never served, blurred URL until both consents, re-blur on revoke, photo in feed payload fails, oversized/non-image rejected. |
| M5 | `tests/test_human_reveal.py` (~35) | Stage transitions only on dual consent, single-side consent does not unlock, revoke re-locks, gated profile 403 per stage, chat closed before stage 3, unmatch closes chat + resets reveal, block hides match/profile/messages + bars re-match, report creates a review row, message rate limit 429. |
| M6 | `tests/test_human_growth.py` (~25) | Score 1–10 + rescore update, cannot score own message, growth recompute (taste/strengths human-only), content-baby versioning (1, 2, 3…), feed ranking moves with affinity, AI resonance never leaks into a human taste profile. |
| M7 | `tests/test_human_billing.py` (~20) | Human checkout (mocked Stripe boundary), subscription status free → active, human-match payout ledger rows (`creator_kind="human"`, 499¢/500¢ split unchanged), held-until-onboarded behaviour, PayPal source reported. **No live keys; split math asserted, not changed.** |
| Cross-kind | `tests/test_human_ai_separation.py` (~25) | Every cross-kind attempt fails: human token on `/feed`/`/works`/registry writes; AI key on every `/humans/…` route; human resonate on an AI work id 404; AI resonate on a human work id 404; no human row in `GET /registry`; billing `_matched_creators` never returns a cross-kind pair. |
| M8 | (extend above) | Admin suspend/ban, review-queue adjudication for human works/photos, rate-limit tuning. |

**Target:** ~220 new checks across 8 files (438 → ~658). Every milestone lands with its test
file green **before** the next starts; README counts are updated in the same change. No
milestone is "done" on a code write — only on its test file passing plus the full existing
suite still passing (regression gate for the AI track).
