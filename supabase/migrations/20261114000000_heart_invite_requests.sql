-- HeArt — invented by John Davis.
-- Migration 15: invite_requests table for the landing-page invite capture
-- (Nov 10, 2026). HeArt v1 is invite-only; the /subscribe page collects
-- emails and stores them here. NO email is sent yet — rows wait until a
-- spot opens and an invite code is issued by hand.
--
-- The endpoint (POST /invite-request) also creates this table itself with
-- CREATE TABLE IF NOT EXISTS if it is missing, so the live site never
-- 500s on a capture before this migration is applied; this file is the
-- canonical record and what scripts/apply_migrations.py applies.

create table if not exists invite_requests (
  id         uuid primary key,
  email      text not null unique,
  created_at timestamptz not null default now()
);

create index if not exists ix_invite_requests_email on invite_requests (email);
