-- HeArt — invented by John Davis.
-- Migration 11: PayPal subscriptions (HeArt Premium $9.99/month via
-- PayPal's own Subscriptions REST API, Nov 9, 2026).
-- John's call (Nov 9): cards + PayPal is enough. Stripe-native PayPal
-- is Europe-only and this is a US account, so PayPal handles the
-- recurring billing itself (see app/paypal.py). PayPal owns the
-- subscription; this table is the local mirror so the app can answer
-- "is this agent premium?" and reconcile each monthly payment into
-- the 50/50 payout ledger (each PAYMENT.SALE.COMPLETED becomes a
-- synthetic InvoiceRecord namespaced 'paypal:<sale id>').
--
-- ADDITIVE ONLY: one new table, no changes to existing tables, no data
-- migration. Safe to apply with the app stopped (same rule as migration 5).
-- Money is integer USD cents in the payout ledger (the sale amount is
-- $9.99 = 999c); this table stores PayPal's own ids and statuses.
-- Status follows PayPal: APPROVAL_PENDING | APPROVED | ACTIVE |
-- SUSPENDED | CANCELLED | EXPIRED. Only ACTIVE counts as premium.
--
-- TEST MODE / SANDBOX: the module talks to api-m.sandbox.paypal.com
-- only and refuses the live host while PAYPAL_TEST_MODE_ONLY is on.

create table paypal_subscriptions (
  id text primary key,
  agent_id uuid not null references agents(id) on delete cascade,
  paypal_subscription_id text not null unique,  -- I-... from PayPal
  paypal_plan_id text not null,                  -- P-... $9.99/month plan
  status text not null default 'APPROVAL_PENDING',  -- PayPal status
  current_period_end timestamptz,                -- next billing time from PayPal; NULL until activated
  paypal_event_id text,                          -- last processed webhook event id (idempotency)
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index idx_paypal_subscriptions_agent_id on paypal_subscriptions(agent_id);
create index idx_paypal_subscriptions_status on paypal_subscriptions(status);

comment on table paypal_subscriptions is 'PayPal subscriptions for HeArt Premium ($9.99/month, Nov 9). PayPal owns the recurring billing; this is the local mirror. Only ACTIVE counts as premium. Each PAYMENT.SALE.COMPLETED mirrors into the 50/50 payout ledger via a synthetic invoice paypal:<sale id>. No PayPal Payouts are made — ledger mirroring only.';
