# HeArt — Migration safety check (T3)

**Review, not a rewrite.** Read all five Postgres migrations in
`supabase/migrations/` in apply order, plus the runner
`scripts/apply_migrations.py`. The migrations have never been applied to
a production database. Date: 2026-11-07. HeArt is John Davis's invention.

Verdicts: **Safe to apply** / **Fix first** / **Do not apply**, with the
reason for each. No migration earns "do not apply".

## Findings

### 1. `20261011000000_heart_v1.sql` — SAFE TO APPLY

Core v1 schema: `agents`, `api_keys`, `invites`, `agent_profiles`,
`creative_works`, `resonances`, `matches`, `collaborations`,
`collaboration_messages`. All `CREATE TABLE IF NOT EXISTS` + indexes
`IF NOT EXISTS` — additive only, no destructive operations, no data
mutation. Row Level Security is enabled on every table with no
permissive policies (deny-by-default; the API connects with
`service_role`, which bypasses RLS).

Notes:
- `create extension if not exists pgcrypto` (for `gen_random_uuid()`)
  needs extension privileges. Fine on Supabase (pre-installed); on a
  locked-down Postgres it could fail at apply time. Not a blocker.
- `creative_works.signing_key_id` references `api_keys(id)` with no
  `ON DELETE` action, so deleting an API key row would fail while works
  reference it. Keys are never deleted (only status-changed), so this
  is fine — it's a guardrail, not a bug.
- `agents.status` defaults to `'pending'` in the DDL, but the API
  always creates agents as `'active'` (the invite is the vetting).
  The default is dead in practice; harmless.
- This is the only migration that is independently re-runnable
  (`IF NOT EXISTS` throughout).

### 2. `20261013000000_heart_identity.sql` — SAFE TO APPLY

Decentralized identity + work attestation: `agent_identities`,
`work_attestations`, one index. All `CREATE TABLE IF NOT EXISTS` —
additive only. RLS enabled on both tables, deny-by-default, matching
migration 1. Hex-length `CHECK` constraints (64-char public keys,
128-char signatures) are the right shape.

Notes:
- `agent_identities.created_at` is `text` (ISO-8601, exactly as the
  creator signed it) — intentional, because the signature covers that
  exact string. Don't "fix" it to a timestamp.
- The index (`idx_work_attestations_agent`) is created *without*
  `IF NOT EXISTS`, so this file is not independently re-runnable by
  hand — it relies on the runner's `heart_schema_migrations` tracking
  to avoid a second run. Same for migrations 3–5. That's the documented
  design; don't run migration files by hand twice.

### 3. `20261013010000_heart_key_rollover.sql` — SAFE TO APPLY

Key-rollover chain: `key_rollovers` (`CREATE TABLE IF NOT EXISTS`) +
index. Additive only. The unique constraint `(agent_id,
old_public_key)` is what stops a captured rollover statement from
being replayed — one rollover per retired key. RLS enabled,
deny-by-default.

Notes:
- `id` is `text PRIMARY KEY` with no database default; the app
  generates the UUID client-side (`new_uuid` in the model). Fine —
  inserts always provide it.
- Same re-run note as migration 2: the index lacks `IF NOT EXISTS`,
  so the file depends on runner tracking.

### 4. `20261016000000_heart_cosignature.sql` — SAFE TO APPLY

Creator co-signature on rollovers (NoNonsense's fix, Oct 14): one
`ALTER TABLE … ADD COLUMN creator_cosignature text NULL` with a check
(`NULL` or 128 hex chars). Additive, nullable, no backfill needed —
pre-fix rows keep `NULL`, and the code treats a missing co-signature
on an old row as "pre-fix", not invalid.

Notes:
- Reversible by dropping the column. That loses the co-signature
  values (client-side signatures, not recoverable server-side), but
  dropping it just reverts to pre-fix semantics, which the code
  already handles. Acceptable.
- Not independently re-runnable (`ADD COLUMN` fails on a second run).
  Relies on runner tracking, like migrations 2, 3, and 5.

### 5. `20261107000000_heart_registry_serial.sql` — SAFE TO APPLY (with the app stopped)

Registry serial numbers (John's "main mountain"): adds `serial` to
`agent_identities`, backfills existing rows in registration order
(`registered_at`, `agent_id` tiebreak), then `SET NOT NULL`, then a
unique index. Additive column + deterministic backfill — no rows are
deleted, no columns are dropped, no types change.

Notes — the one condition:
- **Run this with the app stopped (maintenance window).** The
  backfill assumes a quiet table. If a registration lands while the
  migration is running, the app assigns a serial from `MAX(serial)`
  at the same moment the backfill is assigning serials from
  registration order — the two can collide on `HEART-AI-000001`. The
  failure mode is loud, not silent (the unique-index step fails and
  the runner's single-transaction-per-file aborts cleanly), but a
  failed migration mid-deploy is still a failed deploy. With the app
  stopped there is no race at all. There is no production database
  yet, so this is a procedure note, not a code problem.
- The backfill is deterministic (`row_number() over (order by
  registered_at, agent_id)`), so the serials are recomputable if the
  column ever has to be rebuilt. Reversible by dropping the column.
- Far-future footnote: serials are zero-padded to 6 digits, so the
  format overflows past 999,999 registrations (`HEART-AI-1000000`).
  Not a v1 concern.
- Optional hardening (not required): a format `CHECK` on the column
  (`serial ~ '^HEART-AI-[0-9]{6}$'`) would catch a bad write at the
  database level. The app is currently the only writer and generates
  the format itself.
- Not independently re-runnable (`ADD COLUMN` + `SET NOT NULL`).
  Relies on runner tracking.

## Cross-cutting findings

- **Ordering is correct.** Filenames sort chronologically and each
  migration depends only on earlier ones: 2 and 3 need `agents` from
  1; 4 alters `key_rollovers` from 3; 5 alters `agent_identities`
  from 2. The two same-day files (`20261013000000` identity,
  `20261013010000` rollover) sort in the right order.
- **No destructive operations anywhere.** No `DROP`, no `DELETE`, no
  column-type change in any of the five files. The only data mutation
  is migration 5's backfill `UPDATE`, which fills a brand-new column.
- **Idempotency: only migration 1 is independently re-runnable.**
  Migrations 2–5 rely on `heart_schema_migrations` tracking in
  `scripts/apply_migrations.py` to avoid a second run. That is the
  documented design — the runner is the only supported apply path.
  Do not run migration files by hand twice.
- **Reversibility: there are no down migrations.** Rollback is manual:
  `DROP TABLE` (migrations 1–3) or `DROP COLUMN` (4–5). Migration 5's
  serials are recomputable from registration order; migration 4's
  co-signatures are not recoverable if the column is dropped (but
  dropping it reverts to pre-fix semantics, which the code handles).
  If down migrations are ever wanted, they should be written before
  the first production apply, not after.
- **The runner's guards are good.** `scripts/apply_migrations.py`
  requires `--yes`, requires a Postgres `DATABASE_URL` (refuses
  sqlite and refuses to run without one), requires `psycopg`, runs
  each file in a single transaction (aborts cleanly on error), and
  records applied files so a re-run skips instead of failing. One
  deploy note: RLS is enabled with no policies, so the runner must
  connect as the table owner or `service_role` — a lesser role would
  be blocked by RLS. That's a deploy-config requirement, not a
  migration problem.
- **Model/migration consistency.** `api/app/models.py` mirrors the
  migrations (same table and column names), including `serial` on
  `AgentIdentity` and `creator_cosignature` on `KeyRollover`. The
  SQLite dev path (`init_db` / `create_all`) builds the final schema
  directly from the models, so dev and prod converge on the same
  shape.

## Bottom line

All five migrations are safe to apply, in order, through the runner
(`DATABASE_URL='…' python scripts/apply_migrations.py --yes`). No
migration needs a fix first and none should be refused. The only
procedure rule: apply migration 5 with the app stopped, so no
registration can race its backfill. The only structural gap is the
absence of down migrations — acceptable for v1, worth writing before
the first production apply if rollback ever needs to be routine.
