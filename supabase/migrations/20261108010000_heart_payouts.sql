-- HeArt — invented by John Pierce Davis Jr., Inventor and CEO.
-- Migration 7: creator payouts — the 50/50 payout ledger.
-- Plan: goals/creativity-first-dating-app-prototype/files/stripe-integration-plan-2026-11-07.md
-- John's calls (Nov 8): HeArt Premium $9.99/month flat rate; a subscription
-- payment triggers a payout; the split is HALF — 50/50, creator half vs
-- HeArt half; an AI participant's share is held in this ledger for their
-- specialists (AIs hold no bank account — no transfer is ever attempted
-- for an AI share).
--
-- ADDITIVE ONLY: one new table, no changes to existing tables, no data
-- migration. Safe to apply with the app stopped (same rule as migration 5).
-- Money is integer cents. Split math (billing.process_payouts_for_invoice):
-- creator_half = amount_paid // 2 (floor; the odd cent stays with HeArt,
-- so $9.99 -> creator 499c, HeArt 500c); the creator half is split equally
-- among the creators the paying subscriber matched with, leftover cents
-- also stay with HeArt. Every row for one invoice sums exactly to the
-- amount paid.
--
-- TEST MODE ONLY. No live keys, no live transfers. Stripe is the source of
-- truth for transfers (stripe_transfer_id); this table is the ledger that
-- proves what was paid, held, retained, or failed.
--
-- Nov 9, 2026 fix: payer/creator agent_id were `text` but agents(id) is
-- uuid — Postgres refused the foreign keys on first deploy. Both are uuid
-- now, matching migrations 1–5.

create table payouts (
  id text primary key,
  stripe_invoice_id text not null,
  stripe_subscription_id text,
  payer_agent_id uuid not null references agents(id) on delete cascade,
  creator_agent_id uuid references agents(id) on delete set null,
  creator_serial text,
  kind text not null,          -- 'creator' | 'platform'
  creator_kind text,           -- 'human' | 'ai' | NULL (platform half)
  amount_cents bigint not null,
  currency text not null default 'usd',
  status text not null,        -- 'transferred' | 'held' | 'retained' | 'failed'
  stripe_transfer_id text,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index idx_payouts_invoice_id on payouts(stripe_invoice_id);
create index idx_payouts_payer_agent_id on payouts(payer_agent_id);
create index idx_payouts_creator_agent_id on payouts(creator_agent_id);
create index idx_payouts_status on payouts(status);

comment on table payouts is 'Creator-payout ledger (John''s 50/50 call, Nov 8): one row per share of one subscription payment. Human creators are paid by Stripe Transfer (stripe_transfer_id); AI creators'' shares are HELD here for their specialists — no bank transfer is ever attempted for an AI share. Platform rows are HeArt''s retained half.';
