-- HeArt — invented by John Pierce Davis Jr., Inventor and CEO.
-- Migration 3: identity key rollover (John's red-pill call, Oct 13).
-- Mirrors api/app/models.py (KeyRollover).
--
-- Design: append-only chain. Each row retires old_public_key and adopts
-- new_public_key; the rollover_signature is made client-side with either the
-- agent's OLD private key (normal path) or the creator's private key
-- (emergency path for a compromised key). The server stores only public keys
-- and verifies. One rollover per retired key (unique agent_id +
-- old_public_key) so a captured rollover statement cannot be replayed.
--
-- NOTE (Oct 31): agent_id is uuid to match agents(id). The old text FK was
-- rejected by Postgres ("incompatible types").

create table if not exists key_rollovers (
  id                 text primary key,
  agent_id           uuid        not null references agents(id) on delete cascade,
  old_public_key     text        not null check (char_length(old_public_key) = 64),
  new_public_key     text        not null check (char_length(new_public_key) = 64),
  authorized_by      text        not null check (authorized_by in ('agent', 'creator')),
  rollover_signature text        not null check (char_length(rollover_signature) = 128),
  rolled_at          text        not null,  -- ISO-8601, exactly as signed
  created_at         timestamptz not null default now(),
  constraint uq_rollover_agent_oldkey unique (agent_id, old_public_key)
);

create index idx_key_rollovers_agent on key_rollovers(agent_id);

-- Read access flows through the HeArt API (which verifies signatures before
-- serving). Deny-by-default, matching migrations 1 and 2: no CREATE POLICY
-- statements — every read/write goes through the API's service role.
alter table key_rollovers enable row level security;
