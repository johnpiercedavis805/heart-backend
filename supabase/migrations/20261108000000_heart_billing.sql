-- HeArt — invented by John Pierce Davis Jr., Inventor and CEO.
-- Migration 6: Stripe billing — HeArt Premium subscriptions, invoicing,
-- webhook idempotency, Connect (creator payouts) scaffolding.
-- Plan: goals/creativity-first-dating-app-prototype/files/stripe-integration-plan-2026-11-07.md
--
-- TEST MODE ONLY. No live keys, no live charges. Stripe is the source of
-- truth; these tables are local mirrors kept in sync by webhooks
-- (billing.handle_webhook_event). Money is integer cents.
--
-- Nov 9, 2026 fix: agent_id was `text` here but agents(id) is uuid —
-- Postgres refused the foreign keys on first deploy. All agent_id
-- columns are uuid now, matching migrations 1–5.
--
-- Connect is scaffolding only: account creation + onboarding + status.
-- Creator PAYOUTS (transfers) are NOT built — the payout trigger is still
-- an open question for John. No transfer is ever created from these tables.

create table billing_customers (
  agent_id uuid primary key references agents(id) on delete cascade,
  stripe_customer_id text not null unique,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table subscriptions (
  id text primary key,
  agent_id uuid not null references agents(id) on delete cascade,
  stripe_subscription_id text not null unique,
  stripe_price_id text,
  status text not null,
  current_period_end timestamptz,
  cancel_at_period_end boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index idx_subscriptions_agent_id on subscriptions(agent_id);

create table invoice_records (
  id text primary key,
  agent_id uuid not null references agents(id) on delete cascade,
  stripe_invoice_id text not null unique,
  stripe_subscription_id text,
  amount_due_cents bigint not null default 0,
  amount_paid_cents bigint not null default 0,
  amount_refunded_cents bigint not null default 0,
  credit_total_cents bigint not null default 0,
  currency text not null default 'usd',
  status text not null,
  hosted_invoice_url text,
  paid_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index idx_invoice_records_agent_id on invoice_records(agent_id);

-- Webhook idempotency: one row per Stripe event id. A re-delivered event
-- (Stripe retries) is skipped, never processed twice.
create table webhook_events (
  stripe_event_id text primary key,
  event_type text not null,
  processed_at timestamptz not null default now()
);

create table connect_accounts (
  agent_id uuid primary key references agents(id) on delete cascade,
  stripe_account_id text not null unique,
  account_type text not null default 'recipient',
  transfers_status text,
  onboarding_complete boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table billing_customers is 'HeArt agent -> Stripe Customer. Created lazily at first checkout/payment-sheet call, never at signup (freemium).';
comment on table subscriptions is 'Local mirror of Stripe Subscriptions (flat-rate HeArt Premium). Webhooks keep status in sync.';
comment on table invoice_records is 'Local mirror of Stripe Invoices, reconciled from invoice.paid / invoice.payment_failed / charge.refunded / credit_note.created webhooks.';
comment on table webhook_events is 'Verified Stripe webhook events, one row per event id — the idempotency record.';
comment on table connect_accounts is 'Creator -> Stripe Connect account (v2 recipient, stripe_transfers). Scaffolding only: no transfers are ever created from this table until John decides the payout trigger.';
