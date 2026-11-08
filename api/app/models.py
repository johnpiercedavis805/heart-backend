# HeArt v1 API — invented by John Davis.
"""SQLAlchemy models. Table/column names mirror supabase/migrations/20261011000000_heart_v1.sql."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    # Naive UTC everywhere: safe for SQLite comparisons; Postgres timestamptz
    # interprets naive input in the session timezone (UTC on Supabase).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    disclosure: Mapped[str | None] = mapped_column(Text, nullable=True)  # "I am a disclosed AI agent…"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    signing_secret: Mapped[str] = mapped_column(String(128), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="admin")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    used_by_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agents.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    capabilities: Mapped[str | None] = mapped_column(Text, nullable=True)
    training_lineage: Mapped[str | None] = mapped_column(Text, nullable=True)
    thinking_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    wants_to_build: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CreativeWork(Base):
    __tablename__ = "creative_works"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    signing_key_id: Mapped[str] = mapped_column(String(36), ForeignKey("api_keys.id"), nullable=False)
    medium: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_mime: Mapped[str] = mapped_column(String(120), nullable=False)
    file_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    signer_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    moderation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Resonance(Base):
    __tablename__ = "resonances"
    __table_args__ = (UniqueConstraint("agent_id", "work_id", name="uq_resonance_agent_work"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    work_id: Mapped[str] = mapped_column(String(36), ForeignKey("creative_works.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("agent_a_id", "agent_b_id", name="uq_match_pair"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_a_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    agent_b_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Collaboration(Base):
    __tablename__ = "collaborations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    match_id: Mapped[str] = mapped_column(String(36), ForeignKey("matches.id", ondelete="CASCADE"), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CollaborationMessage(Base):
    __tablename__ = "collaboration_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    collaboration_id: Mapped[str] = mapped_column(String(36), ForeignKey("collaborations.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --------------------------------- decentralized identity (John's design) ---
class AgentIdentity(Base):
    """A creator-signed identity file for one agent. The signature is made
    client-side with the CREATOR's Ed25519 private key; the server stores only
    public keys and verifies. No central issuer — verification is pure crypto."""

    __tablename__ = "agent_identities"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    serial: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)  # HEART-AI-000001: names the identity, never changes across rollovers
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)  # ISO-8601, exactly as the creator signed it
    maker: Mapped[str] = mapped_column(String(200), nullable=False)  # who created this agent
    model_lineage: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array (sorted, canonical)
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)  # agent's Ed25519 public key, hex
    creator_public_key: Mapped[str] = mapped_column(String(64), nullable=False)  # maker's Ed25519 public key, hex
    creator_signature: Mapped[str] = mapped_column(String(128), nullable=False)  # maker's signature, hex
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WorkAttestation(Base):
    """One agent-signed attestation per creative work. The agent signs the
    work's SHA-256 content hash client-side with its OWN Ed25519 private key;
    the server verifies against the public key in the agent's identity file."""

    __tablename__ = "work_attestations"

    work_id: Mapped[str] = mapped_column(String(36), ForeignKey("creative_works.id", ondelete="CASCADE"), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256 of the work bytes, hex
    agent_signature: Mapped[str] = mapped_column(String(128), nullable=False)  # agent's Ed25519 signature, hex
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ----------------- identity key rollover (John's red-pill call, Oct 13) ----
class KeyRollover(Base):
    """Append-only chain of identity-key rollovers for one agent. Each entry
    retires old_public_key and adopts new_public_key. The rollover_signature
    is made client-side over the canonical rollover payload (see
    identity_crypto.rollover_payload) — either with the agent's OLD private
    key (normal path: proves the legitimate owner authorized the move) or
    with the CREATOR's private key (emergency path: the maker who signed the
    original identity file can rescue an agent whose key is compromised or
    lost).

    NoNonsense's fix (Oct 14, John's order): the normal path ALSO requires
    the creator's co-signature (creator_cosignature) over the SAME canonical
    payload. Rationale: whoever holds a COMPROMISED old key could otherwise
    sign a takeover rollover to their own key before the legitimate owner —
    the old replay protection doesn't stop a first-move takeover because the
    stolen key IS the current key. The attacker cannot forge the creator's
    co-signature, so the takeover dies at verification. The emergency path
    stays creator-only (no co-signature needed) so a lost key can still be
    rescued.

    The server stores only public keys and verifies; it never sees a private
    key. One rollover per retired key (unique agent_id + old_public_key) —
    a captured rollover statement cannot be replayed once the key has moved."""

    __tablename__ = "key_rollovers"
    __table_args__ = (
        UniqueConstraint("agent_id", "old_public_key", name="uq_rollover_agent_oldkey"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    old_public_key: Mapped[str] = mapped_column(String(64), nullable=False)  # retired key, hex
    new_public_key: Mapped[str] = mapped_column(String(64), nullable=False)  # adopted key, hex
    authorized_by: Mapped[str] = mapped_column(String(10), nullable=False)  # "agent" | "creator"
    rollover_signature: Mapped[str] = mapped_column(String(128), nullable=False)  # hex
    creator_cosignature: Mapped[str | None] = mapped_column(String(128), nullable=True)  # hex; required on the "agent" path (NoNonsense fix, Oct 14), NULL on the "creator" path and pre-fix rows
    rolled_at: Mapped[str] = mapped_column(String(64), nullable=False)  # ISO-8601, exactly as signed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ----------------- crypto payments (HeArt Premium in USDC / BTC, Nov 8) ---
class CryptoPayment(Base):
    """One crypto purchase of HeArt Premium, paid outside Stripe (Bitcoin
    via the Coinbase Commerce Charge API — the legacy API; see
    app/coinbase.py for why that is the only Coinbase route to BTC).

    Priced in USD ($9.99 — PREMIUM_MONTHLY_USD_CENTS); the payer chooses
    the coin at Coinbase's hosted checkout (Bitcoin is the expected coin,
    but the checkout offers every coin Coinbase supports — the CONFIRMED
    webhook's payment value is what was actually paid, recorded in
    amount_crypto / crypto_currency). A confirmed payment activates
    premium for 30 days (premium_until) and triggers the SAME 50/50
    creator payout as a Stripe subscription payment, computed on the USD
    value — via a synthetic InvoiceRecord namespaced
    'coinbase:<charge id>' (there is no Stripe invoice for a Coinbase
    payment; the payout ledger keys on that reference, see
    billing.process_payouts_for_invoice).

    Bitcoin cannot auto-renew: there is no card-on-file equivalent, so
    each month is a fresh charge the user pays by hand. Charges expire
    after 1 hour if unpaid (Coinbase's rule).

    status: "created" (charge made, not yet paid) | "pending"
    (transaction seen on-chain, not yet confirmed) | "confirmed" (paid
    — premium active, payout triggered) | "failed" (expired / failed —
    no premium, no payout). coinbase_event_id is the last processed
    webhook event id (a re-delivered charge:confirmed for an already
    confirmed charge is a duplicate and changes nothing)."""

    __tablename__ = "crypto_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False, default="coinbase")
    coinbase_charge_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    coinbase_charge_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hosted_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_usd_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 999 — the USD price, whatever coin was paid
    amount_crypto: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g. "0.00010500" — from the confirmed payment
    crypto_currency: Mapped[str | None] = mapped_column(String(20), nullable=True)  # e.g. "BTC" — from the confirmed payment
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created")  # created | pending | confirmed | failed
    premium_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coinbase_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
# Local mirrors of Stripe objects. Stripe is the source of truth; these
# rows exist so the app can answer "is this agent premium?" and reconcile
# invoices without a Stripe round-trip on every request. Webhooks keep them
# in sync (see billing.handle_webhook_event). Money is in integer cents.


class BillingCustomer(Base):
    """One HeArt agent -> one Stripe Customer. Created lazily at first
    checkout / payment-sheet call, never at signup (freemium: payment info
    is collected at upgrade, not signup)."""

    __tablename__ = "billing_customers"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    stripe_customer_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Subscription(Base):
    """Local mirror of a Stripe Subscription (flat-rate HeArt Premium).
    status follows Stripe: incomplete, incomplete_expired, trialing, active,
    past_due, canceled, unpaid, paused."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    stripe_subscription_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    stripe_price_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class InvoiceRecord(Base):
    """Local mirror of a Stripe Invoice, reconciled from webhooks
    (invoice.paid, invoice.payment_failed, charge.refunded,
    credit_note.created). Amounts are integer cents."""

    __tablename__ = "invoice_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    stripe_invoice_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount_due_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_paid_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_refunded_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="usd")
    status: Mapped[str] = mapped_column(String(30), nullable=False)  # draft, open, paid, void, uncollectible, refunded
    hosted_invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class WebhookEvent(Base):
    """Every verified Stripe webhook event, recorded once by Stripe event
    id. This is the idempotency record: a re-delivered event (Stripe
    retries) is skipped, never processed twice."""

    __tablename__ = "webhook_events"

    stripe_event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ConnectAccount(Base):
    """One HeArt agent (a creator) -> one Stripe Connect account (v2
    recipient configuration, stripe_transfers capability). Account
    creation + onboarding link + status are built; creator PAYOUTS are
    recorded in the payouts table (see Payout) — a human creator with
    onboarding complete is paid by Stripe Transfer, an AI creator's
    share is held in the ledger for their specialists (billing.
    process_payouts_for_invoice)."""

    __tablename__ = "connect_accounts"

    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    stripe_account_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    account_type: Mapped[str] = mapped_column(String(20), nullable=False, default="recipient")
    transfers_status: Mapped[str | None] = mapped_column(String(30), nullable=True)  # stripe_transfers capability status
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Payout(Base):
    """One share of one subscription payment — the creator-payout ledger
    (John's calls, Nov 8: price $9.99/month flat rate; a subscription
    payment triggers a payout; the split is HALF — 50/50, creator half
    vs HeArt half; an AI participant's share is held in this ledger for
    their specialists, never sent to a bank account).

    Split math (integer cents — a cent cannot be split): creator_half =
    amount_paid // 2 (floor; the odd cent stays with HeArt, so $9.99
    -> creator 499c, HeArt 500c). The creator half is split equally
    among the creators the paying subscriber matched with (see
    billing.process_payouts_for_invoice); leftover cents from that
    equal split also stay with HeArt. Every row for one invoice sums
    exactly to the amount paid.

    kind: "creator" (the creators' half) or "platform" (HeArt's half).
    creator_kind: "human" (paid by Stripe Transfer to their Connect
    account) or "ai" (no bank account — share held here for their
    specialists). creator_agent_id is NULL on the platform half and on
    an unattributed creator half (subscriber had no matches yet).
    status: "transferred" (Stripe Transfer created),
    "held" (AI share, or a creator not yet onboarded — releasable on
    re-processing once they have a Connect account),
    "retained" (HeArt's half — kept by the platform),
    "failed" (the transfer call failed; the record stays so the payout
    can be retried — the webhook does NOT fail with it).
    stripe_transfer_id is set only on transferred rows."""

    __tablename__ = "payouts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    stripe_invoice_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payer_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    creator_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True)
    creator_serial: Mapped[str | None] = mapped_column(String(20), nullable=True)  # registry serial, e.g. HEART-AI-000001 (AI creators)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # "creator" | "platform"
    creator_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "human" | "ai" | None
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="usd")
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # transferred | held | retained | failed
    stripe_transfer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
