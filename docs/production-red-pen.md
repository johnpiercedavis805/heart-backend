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

1. **FIX — Signing secrets are stored in plaintext in the database.**
   `api_keys.signing_secret` is a 256-bit HMAC secret stored as hex in the
   `api_keys` table (`api/app/crypto.py`, `supabase/migrations/20261011000000_heart_v1.sql`).
   If the database leaks, anyone can forge provenance signatures for any
   work — the whole "which key signed this" guarantee dies. The code already
   flags it (`PRODUCTION TODO: move to KMS/vault`) and the README calls it
   "acceptable for dev only". Before production: move signing secrets to a
   KMS/vault (or encrypt them at rest with a key that lives outside the DB).
   This is the single biggest secret-handling gap in the codebase.

2. **FIX — A production deploy with no `DATABASE_URL` silently runs on local SQLite.**
   `config.py` defaults `DATABASE_URL` to `sqlite:///./heart.db`, and
   `main.py`'s lifespan runs `init_db()` (create-all) for any SQLite URL.
   If the platform env var is missing or misspelled, the app boots, answers
   health checks, and stores everything in a throwaway local file that
   vanishes on redeploy — silently. No error, no warning. Fail fast instead:
   require `DATABASE_URL` to be set explicitly in production (or require an
   explicit `ALLOW_SQLITE_DEV` flag to use the SQLite default).

3. **FIX — The moderation classifier is a stub, and everything else auto-approves.**
   `api/app/moderation.py` enforces structural rules only (no executables,
   no HTML/SVG, no empty files). Anything passing those rules is
   `approved` — no content judgment happens at all (no detection of
   instructions-to-humans, manipulation, or non-positive content). A public
   launch with this stub means unmoderated content goes straight to the
   feed. Before public launch, either wire in a real classifier or flip
   the default to `review` (human queue) instead of auto-approve.

4. **FIX — The signup rate limiter keys on IP, but uvicorn doesn't trust proxy headers.**
   `POST /signup` is limited to 10/hour per IP (`routes.py`,
   `signup_limiter`). Behind Render/Fly, `request.client.host` is the
   proxy's IP unless uvicorn is told to trust `X-Forwarded-For` — so the
   limit becomes effectively global (10 signups/hour for everyone) and the
   per-IP keying is wrong. Add `--proxy-headers --forwarded-allow-ips='*'`
   (or the platform's proxy IPs) to the uvicorn `CMD`. The per-key limiter
   is unaffected (it keys on the API-key hash, not IP).

5. **FIX — `.dockerignore` doesn't exclude secrets files.**
   It excludes `.venv/`, `__pycache__/`, `*.db`, `uploads/`, `.git/` — but
   not `.env`, `.env.*`, `*.pem`, or `*.key`. The Dockerfile copies `api/`,
   `scripts/`, and `supabase/`; a `.env` (or a stray `.pem`/`.key`) placed
   inside `api/` would be copied into the production image. Add
   `.env`, `.env.*`, `*.pem`, `*.key` to `.dockerignore`. (`.gitignore`
   already covers them for git — this is the image-side gap.)

## FIX — before scaling, not before first deploy

6. **FIX — `REGISTRY_SIGNING_KEY` unset in production means an ephemeral key.**
   When unset, the server generates a fresh Ed25519 key per process
   (`identity_crypto.py`), so the public key in `/registry/:serial/verify`
   responses changes on every restart and verifiers cannot pin it. Set a
   stable key in production and publish its public key out-of-band. (The
   README documents this; the deploy checklist should enforce it.)

7. **FIX — No `HEALTHCHECK` in the Dockerfile.**
   The app has a database-aware `/health` endpoint (200 when the DB
   answers, 503 when it doesn't), but the image never declares a
   healthcheck, so the container runtime can't restart an unhealthy
   container on its own. Add one (plain `python -c` + `urllib` against
   `localhost:8000/health` — no extra package needed). Render/Fly
   health checks are configured on the platform side, but a Dockerfile
   `HEALTHCHECK` is the portable backstop.

8. **FIX — Base image isn't digest-pinned.**
   `FROM python:3.12-slim` floats with the tag. A rebuild months from now
   can silently pull a different base layer. Pin to a digest
   (`python:3.12-slim@sha256:…`) for reproducible builds.

9. **FIX — `/docs` and `/openapi.json` are enabled by default.**
   FastAPI serves interactive API docs publicly. That exposes the full
   endpoint surface (paths, schemas, auth shapes) on an invite-only
   service. Either disable docs in production (`docs_url=None`,
   `redoc_url=None`) or leave them on deliberately — but make it a
   decision, not an accident.

10. **FIX — No deploy config in the repo.**
    There is no `render.yaml`, `fly.toml`, or `Procfile`, and no
    `.env.example` (the `.gitignore` even anticipates one with
    `!.env.example`, but the file doesn't exist). The deploy — port,
    health-check path, env vars, persistent disk — lives in a dashboard,
    not in version control, so a rebuild from scratch is guesswork. Add a
    `render.yaml` (or `fly.toml`) and a `.env.example` listing every
    required variable.

11. **CUT — Tests ship in the production image.**
    `api/tests/` isn't excluded by `.dockerignore`, so the test scripts
    ride into the image. Harmless, but bloat. Add `api/tests/` (or
    `**/tests/`) to `.dockerignore`.

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
deploy — but items 1–5 are the ones that bite in production. Item 1
(signing secrets in the DB) and item 2 (silent SQLite fallback) are the
two that can silently destroy the trust story HeArt is built on; fix
those before any real users arrive. Items 3–5 are launch config, not
code rewrites. The rest is hygiene.
