-- HeArt — invented by John Davis.
-- Migration 8: crypto payments (Bitcoin via Coinbase Commerce Charge API).
-- Plan: goals/creativity-first-dating-app-prototype/files/stripe-integration-plan-2026-11-07.md
-- John's calls (Nov 8): users can pay with crypto — USDC via Stripe AND
-- Bitcoin via Coinbase. HeArt Premium is $9.99/month; a confirmed BTC
-- payment activates premium for 30 days and triggers the SAME 50/50
-- creator payout as a Stripe subscription payment, computed on the USD
-- value (see billing.process_payouts_for_invoice, via a synthetic
-- InvoiceRecord namespaced 'coinbase:<charge id>').
--
-- ADDITIVE ONLY: one new table, no changes to existing tables, no data
-- migration. Safe to apply with the app stopped (same rule as migration 5).
-- Money is integer USD cents (amount_usd_cents = 999); amount_crypto is
-- the coin amount actually paid (a string — BTC has 8 decimals, and the
-- payer chooses the coin at Coinbase's hosted checkout).
--
-- TEST MODE / SANDBOX LIMIT: there is NO sandbox for the Commerce Charge
-- API (Coinbase never shipped one). Verification is mocked at the HTTP
-- boundary (api/tests/test_crypto.py) plus the hosted checkout with a
-- real key. See app/coinbase.py — the Charge API is the legacy API and
-- the only Coinbase route to Bitcoin; the current Checkouts API is
-- USDC-only.

create table crypto_payments (
  id text primary key,
  agent_id text not null references agents(id) on delete cascade,
  provider text not null default 'coinbase',
  coinbase_charge_id text not null unique,
  coinbase_charge_code text,
  hosted_url text,
  amount_usd_cents bigint not null,   -- 999 — the USD price, whatever coin was paid
  amount_crypto text,                 -- e.g. '0.00010500' — from the confirmed payment
  crypto_currency text,               -- e.g. 'BTC' — from the confirmed payment
  status text not null default 'created',  -- 'created' | 'pending' | 'confirmed' | 'failed'
  premium_until timestamptz,          -- 30 days from confirmation; NULL until confirmed
  coinbase_event_id text,             -- last processed webhook event id (idempotency)
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index idx_crypto_payments_agent_id on crypto_payments(agent_id);
create index idx_crypto_payments_status on crypto_payments(status);

comment on table crypto_payments is 'Crypto purchases of HeArt Premium (Bitcoin via Coinbase Commerce Charge API, Nov 8). A confirmed payment activates premium for 30 days (premium_until) and triggers the 50/50 creator payout on the USD value. Bitcoin cannot auto-renew — each month is a fresh charge the user pays by hand. Charges expire after 1 hour if unpaid.';
