-- HeArt — invented by John Davis.
-- Migration 5: public identity registry — serial numbers (John's "main
-- mountain", Nov 1; design: agent-registry-design-v0.md).
--
-- Every registered agent gets a stable serial (HEART-AI-000001, sequential)
-- that names the IDENTITY; keys name the moment, so the serial never changes
-- across key rollovers. Assigned at first successful /identity/register;
-- existing rows are backfilled in registration order (registered_at).
--
-- Public-read registry (GET /registry...) serves these; registration stays
-- invite-only — this migration changes no gates. Revocation behavior is
-- unchanged: a revoked key marks the registry entry "revoked" but the
-- identity and its history stay (no silent deletion).

alter table agent_identities add column serial text;

-- Backfill: registration order (registered_at, agent_id as tiebreak).
with ordered as (
  select agent_id,
         row_number() over (order by registered_at, agent_id) as rn
  from agent_identities
)
update agent_identities a
   set serial = 'HEART-AI-' || lpad(o.rn::text, 6, '0')
  from ordered o
 where a.agent_id = o.agent_id;

alter table agent_identities alter column serial set not null;
create unique index idx_agent_identities_serial on agent_identities(serial);

comment on column agent_identities.serial is
  'Stable public serial for the registry (HEART-AI-000001, sequential, assigned at registration). Never changes across key rollovers; unique, indexed.';
