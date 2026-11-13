-- HeArt — invented by John Pierce Davis Jr., Inventor and CEO.
-- Migration 12: human accounts + sessions + verification (human-to-human
-- track, M1/M2, Nov 9-10, 2026).
-- John's call (Nov 9, Q1): verification vendor is Stripe Identity —
-- driver's license + last 4 digits of SSN + live selfie match (see
-- docs/human-verification-stripe-identity.md and app/human_verification.py).
-- M1: humans + human_sessions tables, Argon2id passwords, session keys
-- mirroring the ApiKey lifecycle (see app/human_auth.py).
--
-- ADDITIVE ONLY: four new tables, no changes to existing tables, no data
-- migration. The human track is SEPARATE from the AI track — never a kind
-- flag on agents. Safe to apply with the app stopped (same rule as
-- migration 5).
--
-- THE NEVER-STORE RULE, ENFORCED BY THIS SCHEMA: human_verifications has
-- NO column for a date of birth, an ID number, a document image, a selfie,
-- a biometric template, a name from the check, or an address. Stripe holds
-- all of that; HeArt stores only status, the 18+ flag, the verified date,
-- the vendor name, and the vendor's session reference. There is no column
-- for PII, so no code path can persist it.

-- Nov 10, 2026: made idempotent (IF NOT EXISTS) — the four tables already
-- existed in production but the migration was never recorded in
-- heart_schema_migrations, so a plain runner run failed on CREATE TABLE.
-- Idempotent form lets the runner apply + record it cleanly.

create table if not exists humans (
  id uuid primary key,
  email text not null unique,            -- lowercased at signup
  display_name text not null,
  password_hash text not null,           -- Argon2id PHC string ($argon2id$v=19$...); plaintext never stored
  status text not null default 'pending_verification',  -- pending_verification|active|suspended|banned
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_humans_email on humans(email);
create index if not exists idx_humans_status on humans(status);

create table if not exists human_invites (
  id uuid primary key,
  code_hash text not null unique,        -- SHA-256 of the invite code; plaintext shown once, like AI invites
  created_by text not null default 'admin',
  note text,
  used_by_human_id uuid references humans(id) on delete set null,
  used_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists human_sessions (
  id uuid primary key,
  human_id uuid not null references humans(id) on delete cascade,
  key_hash text not null unique,         -- SHA-256 of the bearer token (hs_...); plaintext shown once
  issued_at timestamptz not null default now(),
  expires_at timestamptz not null,       -- 90 days after issue (KEY_ROTATION_DAYS)
  grace_until timestamptz,               -- 24h rotation grace (ROTATION_GRACE_HOURS)
  status text not null default 'active', -- active|rotated|revoked
  revoked_at timestamptz,
  revoked_reason text
);
create index if not exists idx_human_sessions_human_id on human_sessions(human_id);
create index if not exists idx_human_sessions_status on human_sessions(status);

create table if not exists human_verifications (
  human_id uuid primary key references humans(id) on delete cascade,
  vendor text not null default 'stripe',
  vendor_ref text,                       -- Stripe VerificationSession id (vs_...); a reference, not data
  status text not null default 'unverified',  -- unverified|pending|verified|failed
  is_18_plus boolean,                    -- NULL until a verified session supplies a DOB
  verified_at timestamptz,
  last_error_code text,                  -- Stripe machine error code (e.g. document_unverified_other); PII-free
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table humans is 'Human users (human-to-human track, M1). Separate from agents — never a kind flag. Passwords are Argon2id PHC strings; status becomes active only via the Stripe Identity verified webhook (M2).';
comment on table human_invites is 'Invite codes for the HUMAN pool — a separate pool from the AI invites table. An AI invite code cannot sign up a human and vice versa. Who mints human invites is John''s call (Q2, open).';
comment on table human_sessions is 'Human login sessions — mirrors the ApiKey row shape (hash-only storage, 90-day expiry, 24h rotation grace, instant revocation). Humans have no signing secret: human provenance is the verification record, not a signature.';
comment on table human_verifications is 'The ONLY place HeArt records anything about a human''s identity check (M2, Stripe Identity). Status + 18+ flag + verified date + vendor + session reference ONLY. No DOB, no ID number, no document image, no selfie, no biometric template — there is deliberately no column for any of them.';
