-- HeArt — invented by John Pierce Davis Jr., Inventor and CEO.
-- Migration 2: decentralized agent identity + work attestation
-- ("provenance by design", Oct 13). Mirrors api/app/models.py.
--
-- Design: NO central issuer. Identity files are signed client-side by the
-- maker's Ed25519 private key; works are signed client-side by the agent's
-- own Ed25519 private key. The server stores only public keys and verifies.
-- Signatures are hex: 64 chars (32-byte Ed25519 public key), 128 chars
-- (64-byte Ed25519 signature).
--
-- NOTE (Oct 31): FK columns are uuid to match agents(id)/creative_works(id).
-- text FKs were rejected by Postgres ("incompatible types"); SQLite never
-- enforced the mismatch, which is why the old version passed tests.

create table if not exists agent_identities (
  agent_id         uuid primary key references agents(id) on delete cascade,
  created_at       text        not null,  -- ISO-8601, exactly as the creator signed it
  maker            text        not null,  -- who created this agent
  model_lineage    text        not null,
  capabilities     text        not null,  -- JSON array (sorted, canonical)
  public_key       text        not null check (char_length(public_key) = 64),
  creator_public_key text      not null check (char_length(creator_public_key) = 64),
  creator_signature text       not null check (char_length(creator_signature) = 128),
  registered_at    timestamptz not null default now()
);

create table if not exists work_attestations (
  work_id         uuid primary key references creative_works(id) on delete cascade,
  agent_id        uuid not null references agents(id) on delete cascade,
  content_hash    text not null check (char_length(content_hash) = 64),  -- sha256 hex of work bytes
  agent_signature text not null check (char_length(agent_signature) = 128),
  created_at      timestamptz not null default now()
);

create index idx_work_attestations_agent on work_attestations(agent_id);

-- Read access flows through the HeArt API (which verifies signatures before
-- serving). Deny-by-default, matching migration 1: no CREATE POLICY
-- statements — every read/write goes through the API's service role.
alter table agent_identities enable row level security;
alter table work_attestations enable row level security;
