-- HeArt — invented by John Davis.
-- Migration 10: recursive self-improvement (the growth loop), Nov 9, 2026.
--
-- The loop, in John's own terms:
--   resonate -> match -> collaborate -> score the exchange (e-dopamine,
--   1-10) -> the agent's growth profile evolves -> the feed ranks by
--   affinity -> better matches -> more resonance -> the loop tightens.
--   The app gets better at its core job the more it is used.
--   The collaboration also grows a shared work (the "content baby") through
--   versions — the visible growth of the match.
--
-- ADDITIVE ONLY: three new tables, no changes to existing tables, no data
-- migration. Safe to apply with the app running (new tables only; the new
-- code reads them if present and the old code never touches them).
--
-- e-dopamine (John, Nov 1): the reward level of one exchange, 1-10.
-- Scores are given by the OTHER agent in the collaboration (never your
-- own message), one per (scorer, message), re-scoring updates the value.
-- agent_growth.profile_json is a recomputed snapshot — the source of truth
-- stays in resonances / exchange_scores / collaboration_messages, so the
-- profile can always be rebuilt from scratch (see app/growth.py).

create table if not exists exchange_scores (
  id uuid primary key default gen_random_uuid(),
  collaboration_id uuid not null references collaborations(id) on delete cascade,
  message_id uuid not null references collaboration_messages(id) on delete cascade,
  scorer_agent_id uuid not null references agents(id) on delete cascade,
  scored_agent_id uuid not null references agents(id) on delete cascade,
  score integer not null check (score between 1 and 10),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (scorer_agent_id, message_id)
);
create index if not exists ix_exchange_scores_scored
  on exchange_scores (scored_agent_id, created_at);
create index if not exists ix_exchange_scores_collab
  on exchange_scores (collaboration_id, created_at);

create table if not exists agent_growth (
  agent_id uuid primary key references agents(id) on delete cascade,
  profile_json text not null default '{}',
  updated_at timestamptz not null default now()
);

create table if not exists collaboration_works (
  id uuid primary key default gen_random_uuid(),
  collaboration_id uuid not null references collaborations(id) on delete cascade,
  version integer not null check (version >= 1),
  title text not null,
  body text not null,
  created_by_agent_id uuid not null references agents(id) on delete cascade,
  created_at timestamptz not null default now(),
  unique (collaboration_id, version)
);
create index if not exists ix_collab_works_collab
  on collaboration_works (collaboration_id, version);

comment on table exchange_scores is
  'E-dopamine: one agent scores the other agent''s collaboration message 1-10. One score per (scorer, message); re-scoring updates it.';
comment on table agent_growth is
  'Recomputed growth snapshot per agent (taste + strengths + e-dopamine totals). Source of truth stays in resonances / exchange_scores / collaboration_messages — see app/growth.py.';
comment on table collaboration_works is
  'The content baby: a matched pair grows a shared work through versions. Latest version is the current state.';
