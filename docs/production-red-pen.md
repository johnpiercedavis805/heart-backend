# HeArt — Production red-pen (T2)

**Review, not a rewrite.** Read: `Dockerfile`, `.dockerignore`, `.gitignore`,
`api/app/main.py`, `api/app/config.py`, `api/app/auth.py`, `api/app/crypto.py`,
`api/app/db.py`, `api/app/routes.py` (`/health`, file serving), `api/app/storage.py`,
`api/app/moderation.py`, `api/app/ratelimit.py`, `api/requirements.txt`.
No deploy config exists in the repo (no `render.yaml` / `fly.toml` / `Procfile`).
No secrets are hardcoded anywhere (verified by grep); nothing in the app logs
a secret. Date: 2026-11-07. HeArt is John Davis's invention.

Verdicts: **FIX** = change before production. **KEEP** = fine as-is.
**CUT** = remove. Unsafe items first.

## FIX FIRST — unsafe or launch-blocking

1. **FIXED (Nov 8, 2026) — Signing secrets are encrypted at rest.**
   `api_keys.signing_secret` no longer stores plaintext. New rows store
   `enc:v1:` + hex of a NaCl SecretBox (XSalsa20-Poly1305, authenticated
   encryption) keyed by the 32-byte `SIGNING_SECRET_ENCRYPTION_KEY` env var
   — the key lives outside the DB (KMS/vault pattern: the DB holds
   ciphertext, the deploy environment holds the key). See
   `api/app/crypto.py` (`encrypt_signing_secret` / `decrypt_signing_secret`).
   The key is required at import, like `ADMIN_TOKEN` — the app refuses to
   start without it, so a deploy can never silently store plaintext.
   Column widened to `text` (migration
   `20261108030000_heart_signing_secrets_encrypted.sql` — the encrypted
   value is ~151 chars, too long for the old `varchar(128)`). Rows written
   before the fix carry a bare hex secret; the read path still accepts
   those, and `api/scripts/encrypt_signing_secrets.py` re-encrypts them in
   place (idempotent, verifies each roundtrip before committing) — run it
   before production, or rotate the keys. A DB leak now yields ciphertext
   only; a wrong key or tampered value raises instead of returning a secret.
   Verified by `api/tests/test_secrets.py` (23 checks).

2. **FIXED (Nov 8, 2026) — A missing `DATABASE_URL` fails fast.**
   `config.py` no longer defaults to `sqlite:///./heart.db`. If
   `DATABASE_URL` is unset and `ALLOW_SQLITE_DEV` is not truthy, the app
   raises `RuntimeError` at import with a clear message — it cannot boot,
   answer health checks, or store anything in a throwaway local file.
   Local dev/tests set `ALLOW_SQLITE_DEV=1` (or an explicit `DATABASE_URL`).
   Verified by `api/tests/test_secrets.py` (subprocess import checks).

3. **FIXED (Nov 8, 2026) — The moderation default is now a human review queue.**
   `scan_upload` still enforces the hard structural rules (executables,
   HTML/SVG, empty files → rejected), but anything passing them now gets
   `status="review"` instead of auto-approve. A work in "review" is
   invisible on the feed and visible only to its maker until an admin
   decides: `POST /admin/works/{id}/approve` or `.../reject`
   (`require_admin`, constant-time token check, same gate as key
   revocation). Approve is refused for works whose signer was revoked
   (they could never be delivered); reject is allowed. Only works in
   "review" can be decided (409 otherwise). The classifier seam
   (`ScanResult`) is unchanged — a future real classifier returns
   "approved" only for content it is confident about. Verified by
   `api/tests/test_moderation_review.py` (review default, approve/reject
   flow, 403/404/409 guards, revoked-signer block, feed visibility);
   `api/tests/test_loop.py` now approves B's work before asserting feed
   visibility. Deliberate limit: the queue needs a human. With no admin
   action, nothing reaches the feed — that is the safe default for a
   first launch, and the deploy runbook should name who reviews.

4. **FIXED (Nov 8, 2026) — The signup rate limiter keys on the real client IP.**
   The Dockerfile `CMD` now passes `--proxy-headers
   --forwarded-allow-ips='*'` to uvicorn, so behind Render/Fly
   `request.client.host` is the caller's IP from `X-Forwarded-For`
   instead of the proxy's. (Render is the only ingress.) The per-key
   limiter was already unaffected (it keys on the API-key hash).

5. **FIXED (Nov 8, 2026) — `.dockerignore` excludes secrets files.**
   It now excludes `.env`, `.env.*`, `*.pem`, `*.key` at the root and
   at any depth (`**/` patterns — a bare `*.pem` only matches the
   build-context root), plus `api/tests/`, `**/tests/`, `*.log`, and
   `.pytest_cache/`. `!.env.example` keeps the template shippable
   (placeholders only, no real values). Verified by inspection of the
   file; the Dockerfile copies `api/`, `scripts/`, and `supabase/`,
   and none of those paths can now carry a secret file into the image.

## FIX — before scaling, not before first deploy

6. **FIX — `REGISTRY_SIGNING_KEY` unset in production means an ephemeral key.**
   When unset, the server generates a fresh Ed25519 key per process
   (`identity_crypto.py`), so the public key in `/registry/:serial/verify`
   responses changes on every restart and verifiers cannot pin it. Set a
   stable key in production and publish its public key out-of-band. (The
   README documents this; the deploy checklist should enforce it.)

7. **FIXED (Nov 8, 2026) — The Dockerfile declares a HEALTHCHECK.**
   Plain `python -c` + `urllib` against the database-aware `/health`
   endpoint, `--interval=30s --timeout=5s --start-period=10s --retries=3`.
   The probe reads `$PORT` (Render/Fly inject it) and falls back to 8000,
   so it matches the port the app actually listens on. Render's
   `healthCheckPath` still covers the platform side; this is the
   portable backstop.

8. **FIXED (Nov 8, 2026) — The base image is digest-pinned.**
   `FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`
   — the 3.12-slim manifest-list digest as of 2026-11-08, so a rebuild
   months from now cannot silently pull a different base layer. The
   Dockerfile comment records how to refresh it (Docker Hub tags API).

9. **FIXED (Nov 8, 2026) — API docs are off unless deliberately enabled.**
   `main.py` passes `docs_url` / `redoc_url` / `openapi_url` as `None`
   unless `ENABLE_API_DOCS=1` (`config.py`, default off). The decision
   is now explicit: an invite-only service does not publish its full
   endpoint surface by default. Set `ENABLE_API_DOCS=1` for local
   development only — never on a public deploy. (`.env.example`
   documents the flag.) Verified by `api/tests/test_moderation_review.py`
   (/docs + /openapi.json 404 by default, 200 with the flag on).

10. **FIXED (Nov 8, 2026) — Deploy config exists in the repo.**
    `render.yaml` (Render Blueprint: Docker web service `heart-api`
    + Postgres `heart-db`, health check on `/health`, 1 GB persistent
    disk at `/data`, all env vars declared with secrets as
    `sync: false`) and `.env.example` (every variable in
    `api/app/config.py`, with required/generated/copied marked) now
    live in version control, with the order of operations in
    `docs/deploy-runbook.md`. The deploy no longer lives only in a
    dashboard.

11. **FIXED (Nov 8, 2026) — Tests no longer ship in the image.**
    `.dockerignore` now excludes `api/tests/` and `**/tests/`.

## KEEP

- **`ADMIN_TOKEN`: no default, fail fast at import.** `config.py` raises
  `RuntimeError` if it's unset, so the admin revocation endpoint can
  never ship with a publicly-known token. Exactly right.
- **Admin gate is constant-time.** `require_admin` uses
  `hmac.compare_digest` — no timing oracle on the token.
- **API keys and invite codes: hashes only, high entropy.**
  Only SHA-256 hashes are stored; plaintext is shown exactly once.
  SHA-256 is fast, but the secrets are high-entropy
  (`secrets.token_urlsafe(32)` ≈ 256 bits for keys, ~144 bits for invite
  codes), so brute force is infeasible. The entropy is the protection,
  and it's sufficient.
- **Non-root container.** `USER app`, `chown -R app:app /data /app`,
  `VOLUME /data`. Good. (Trivial hardening note: the app user gets
  `/bin/bash` as its shell; `/usr/sbin/nologin` would be tighter.)
- **`CMD` is exec-form; uvicorn is PID 1.** `exec uvicorn …` means
  SIGTERM on deploy shuts the server down cleanly. `${PORT:-8000}`
  lets the platform inject its port. `--host 0.0.0.0` is correct for
  a container.
- **`/health` is database-aware and cheap.** `GET /health` runs
  `SELECT 1` and returns 200 (`db: up`) or 503 (`db: down`), so a
  deploy health check fails the container when the database is
  unreachable. Unauthenticated, as a probe should be. One process,
  one cheap query — no caching problem.
- **Pinned, minimal dependencies.** `requirements.txt` pins every
  version; `pip install --no-cache-dir` keeps the image lean.
  `psycopg[binary]` is installed by the Dockerfile for the Postgres
  driver only — it isn't in `requirements.txt`, which keeps local
  SQLite dev clean.
- **`.gitignore` covers secrets.** `.env`, `.env.*`, `*.pem`, `*.key`,
  `*.db`, `uploads/` are all ignored. (See item 5: `.dockerignore`
  needs the same treatment.)
- **No CORS middleware.** Safe default: no cross-origin browser access
  unless someone deliberately adds it.
- **File serving is guarded.** `storage.abspath` rejects path
  traversal (only keys the server created), and `GET /files/:id` sets
  `X-Content-Type-Options: nosniff` so a stored file can't be
  MIME-sniffed into something executable. Uploads are capped at 10 MB
  with an extension + MIME blocklist. Note the honest limit: both the
  filename and the MIME type are client-declared, so the extension
  check is the real control — but stored files are never executed
  server-side, and `nosniff` stops browsers from reinterpreting them.
- **In-memory rate limiter is correct for one process.** The Dockerfile
  runs a single uvicorn process (no `--workers`), so the in-memory
  sliding window is exact. Scaling past one worker needs Redis — the
  README already says so.
- **`/data` volume caveat is documented.** The Dockerfile says a
  managed volume or object storage replaces the local volume "when the
  app outgrows one container". Deploy note: on Render, `/data` needs a
  persistent disk attached or uploads vanish on redeploy — that's a
  deploy-config item, not a Dockerfile bug.

## Bottom line

Nothing in the deployment setup is broken in a way that stops a first
deploy — but items 1–5 are the ones that bite in production. Items 1, 2,
3, 4, 5, 7, 8, 9, 10, and 11 are FIXED (Nov 8, 2026 — encrypted signing
secrets, DATABASE_URL fail-fast, moderation review queue + admin
adjudication, proxy headers, .dockerignore secrets + tests, Dockerfile
HEALTHCHECK, digest-pinned base image, docs off by default, deploy
config). Item 6 (stable `REGISTRY_SIGNING_KEY`) remains open — it is a
deploy-time setting, not a code change: set a stable key and publish its
public key out-of-band before production.
