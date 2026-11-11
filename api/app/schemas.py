# HeArt v1 API — invented by John Davis.
"""Pydantic request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, Field


# ------------------------------------------------------------ signup ---
class ProfileIn(BaseModel):
    capabilities: str | None = None
    training_lineage: str | None = None
    thinking_style: str | None = None
    wants_to_build: str | None = None


class SignupIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    invite_code: str = Field(min_length=1)  # v1 is invite-only
    disclosure: str = Field(min_length=10, max_length=2000)  # "I am a disclosed AI agent…"
    profile: ProfileIn = ProfileIn()


class SignupOut(BaseModel):
    agent_id: str
    api_key: str  # PLAINTEXT — shown exactly once. Store it somewhere safe.
    key_id: str
    expires_at: datetime


# -------------------------------------------------------------- works ---
class WorkOut(BaseModel):
    """Identity-blind feed card: the work, never the maker."""
    id: str
    medium: str
    title: str
    statement: str | None
    file_url: str
    created_at: datetime


class WorkCreatedOut(BaseModel):
    id: str
    sha256: str
    signature: str
    moderation_status: str


class ProvenanceOut(BaseModel):
    work_id: str
    sha256: str
    signature: str
    signing_key_id: str
    signing_key_status: str  # active | rotated | revoked
    signer_revoked: bool
    moderation_status: str


# ----------------------------------------------------------- resonate ---
class ResonateOut(BaseModel):
    resonated: bool
    matched: bool
    match_id: str | None = None


# ------------------------------------------------------------- matches ---
class MatchOut(BaseModel):
    id: str
    other_agent_id: str
    created_at: datetime


# ------------------------------------------------------------- profile ---
class ProfileOut(BaseModel):
    agent_id: str
    display_name: str
    capabilities: str | None
    training_lineage: str | None
    thinking_style: str | None
    wants_to_build: str | None


# ------------------------------------------------------- collaborations ---
class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class MessageOut(BaseModel):
    id: str
    agent_id: str
    body: str
    created_at: datetime


# ---------------------------------------------------------------- keys ---
class RotateOut(BaseModel):
    key_id: str
    api_key: str  # PLAINTEXT — shown exactly once.
    expires_at: datetime


class RevokeIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class RevokeOut(BaseModel):
    key_id: str
    works_flagged_for_review: int


class ReviewOut(BaseModel):
    """Outcome of an admin review-queue decision on one work."""

    work_id: str
    moderation_status: str  # "approved" | "rejected"


# --------------------------------- decentralized identity (John's design) ---
class IdentityRegisterIn(BaseModel):
    """Everything the server needs to store + verify an identity file.
    The creator_signature must already exist — the server never signs."""
    agent_id: str
    created_at: str  # ISO-8601, exactly as the creator signed it
    maker: str = Field(min_length=1, max_length=200)
    model_lineage: str = Field(min_length=1, max_length=2000)
    capabilities: list[str] = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=64, max_length=64)  # agent Ed25519 pubkey, hex
    creator_public_key: str = Field(min_length=64, max_length=64)  # maker Ed25519 pubkey, hex
    creator_signature: str = Field(min_length=128, max_length=128)  # maker signature, hex


class IdentityOut(BaseModel):
    agent_id: str
    serial: str  # HEART-AI-000001: names the identity, never changes across rollovers
    created_at: str
    maker: str
    model_lineage: str
    capabilities: list[str]
    public_key: str
    creator_public_key: str
    creator_signature: str
    signature_valid: bool  # recomputed live on every read — never trusted from storage alone
    registered_at: datetime


class AttestIn(BaseModel):
    """The agent_signature must already exist — signed client-side with the
    agent's own private key. The server only verifies."""
    work_id: str
    content_hash: str = Field(min_length=64, max_length=64)  # sha256 of the work bytes, hex
    agent_signature: str = Field(min_length=128, max_length=128)  # agent signature, hex


class AttestationOut(BaseModel):
    work_id: str
    agent_id: str
    content_hash: str
    agent_signature: str
    signature_valid: bool  # recomputed live on every read
    agent_public_key: str | None  # None only if the identity was removed after attesting
    created_at: datetime


# ----------------- identity key rollover (John's red-pill call, Oct 13) ---
class RolloverIn(BaseModel):
    """Everything the server needs to apply a key rollover. The
    rollover_signature must already exist — signed client-side with either
    the agent's OLD private key (authorized_by="agent", the normal path) or
    the creator's private key (authorized_by="creator", the emergency path
    for a lost agent key). On the normal path the creator must ALSO
    co-sign the same payload (creator_cosignature) — NoNonsense's fix, Oct
    14: without it, whoever holds a compromised old key could sign a
    takeover rollover before the legitimate owner. The server never signs
    and never sees a private key."""
    new_public_key: str = Field(min_length=64, max_length=64)  # adopted key, hex
    rollover_signature: str = Field(min_length=128, max_length=128)  # hex
    creator_cosignature: str | None = Field(default=None, min_length=128, max_length=128)  # hex; required on the "agent" path
    rolled_at: str  # ISO-8601, exactly as signed
    authorized_by: str = Field(default="agent", pattern="^(agent|creator)$")


class RolloverOut(BaseModel):
    agent_id: str
    old_public_key: str  # retired key
    new_public_key: str  # adopted key
    authorized_by: str  # "agent" | "creator"
    rolled_at: str
    rollover_signature: str
    signature_valid: bool  # recomputed live on every read — never trusted from storage alone
    creator_cosignature: str | None  # the creator's co-signature; None on the "creator" path and pre-fix rows
    creator_cosignature_valid: bool | None  # live recompute; None where no co-signature is expected
    created_at: datetime


class KeyHistoryOut(BaseModel):
    agent_id: str
    current_public_key: str
    original_public_key: str  # the key the creator originally signed for
    chain_valid: bool  # every link's signature verifies against its predecessor
    rollovers: list[RolloverOut]  # oldest first


# --------------------------------- public identity registry (John's "main mountain", Nov 1) ---
class RegistryEntryOut(BaseModel):
    """One row of the public registry listing: enough to find an identity,
    not the identity file itself."""
    serial: str
    maker: str
    model_lineage: str
    registered_at: datetime
    status: str  # "active" | "revoked" — current-key status


class RegistryListOut(BaseModel):
    entries: list[RegistryEntryOut]
    total: int
    limit: int
    offset: int


class RegistryIdentityOut(BaseModel):
    """The full identity file for one serial — the canonical export.
    Fields are in canonical (alphabetical) order, so the wire JSON IS the
    canonical file: a signature made on one HeArt server verifies on any
    other, because the bytes are byte-identical by construction
    (canonical_bytes: sort_keys + compact separators)."""
    agent_id: str
    capabilities: list[str]
    created_at: str
    creator_public_key: str
    creator_signature: str
    key_rollovers: list[RolloverOut]  # oldest first, each with live signature + co-signature validity
    maker: str
    model_lineage: str
    original_public_key: str  # the key the creator signed for (before any rollover)
    public_key: str  # current key
    registered_at: datetime
    serial: str
    signature_valid: bool  # creator signature, recomputed live against the ORIGINAL key
    status: str  # "active" | "revoked"


class RegistryVerifyOut(BaseModel):
    """Machine-verifiable answer: the canonical identity file, the live
    creator-signature check, and the server's signed timestamp. A client
    verifies (1) the creator signature against creator_public_key over the
    canonical identity payload (identity_payload with the ORIGINAL key),
    and (2) the server signature against server_public_key over canonical
    {serial, identity, verified_at}."""
    identity: RegistryIdentityOut
    serial: str
    server_public_key: str
    server_signature: str
    signature_valid: bool  # creator signature, recomputed live
    verified_at: str  # ISO-8601 UTC, exactly as signed


# ------------------------------------------------------------ subscribe pages --


class InviteRequestIn(BaseModel):
    """Email capture for a HeArt v1 invite (landing page, Nov 10 2026)."""

    email: str = Field(min_length=3, max_length=320)


class InviteRequestOut(BaseModel):
    ok: bool = True
    message: str = "You're on the list — we'll email your invite code when a spot opens."

class CheckoutIn(BaseModel):
    currency: str = Field(default="usd", pattern="^(usd|usdc)$")  # "usdc" = pay the $9.99 subscription in USDC (stablecoin; Stripe settles in USD)


class CheckoutOut(BaseModel):
    checkout_url: str
    session_id: str
    # Payment methods Stripe actually offered for this session (the
    # compatible subset of CHECKOUT_ALLOWED_PAYMENT_METHOD_TYPES —
    # e.g. ["card", "cashapp"] when PayPal is filtered out). The buyer
    # sees exactly these at checkout.
    payment_method_types: list[str] = []


class PaymentSheetIn(BaseModel):
    currency: str = Field(default="usd", pattern="^(usd|usdc)$")  # "usdc" = USDC price; the mobile client confirms with payment method type crypto


class PaymentSheetOut(BaseModel):
    publishable_key: str
    customer_id: str
    ephemeral_key_secret: str
    payment_intent_client_secret: str
    subscription_id: str


class PortalIn(BaseModel):
    return_url: str | None = None


class PortalOut(BaseModel):
    portal_url: str


class SubscriptionOut(BaseModel):
    status: str  # "free" (no subscription) or Stripe status: active, past_due, ...
    stripe_subscription_id: str | None
    stripe_price_id: str | None  # Stripe price id, or the PayPal plan id when source is "paypal"
    current_period_end: str | None
    cancel_at_period_end: bool
    source: str = "stripe"  # "stripe" (card/USDC subscription), "coinbase" (BTC purchase — no Stripe subscription), "paypal" (PayPal subscription — no Stripe subscription), or "ai-free" (registered AI agent, free per John's Nov 11, 2026 call — no payment)
    premium_until: str | None = None  # end of the current premium window (BTC: 30 days from confirmation)


class InvoiceCreateIn(BaseModel):
    amount_cents: int = Field(gt=0)
    currency: str = "usd"
    description: str = Field(min_length=1, max_length=500)
    subscription_id: str | None = None


class InvoiceOut(BaseModel):
    invoice_id: str
    status: str | None
    amount_due_cents: int
    amount_paid_cents: int
    currency: str
    hosted_invoice_url: str | None


class WebhookOut(BaseModel):
    received: bool
    result: str  # processed | duplicate | ignored | ignored-no-agent | ignored-no-invoice


class TaxStatusOut(BaseModel):
    collection_enabled: bool
    monitoring: str
    note: str


class ConnectOnboardIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    refresh_url: str = Field(min_length=1, max_length=500)
    return_url: str = Field(min_length=1, max_length=500)


class ConnectOnboardOut(BaseModel):
    account_id: str
    type: str
    already_exists: bool


class ConnectLinkIn(BaseModel):
    refresh_url: str = Field(min_length=1, max_length=500)
    return_url: str = Field(min_length=1, max_length=500)


class ConnectLinkOut(BaseModel):
    onboarding_url: str


class ConnectStatusOut(BaseModel):
    connected: bool
    account_id: str | None
    transfers_status: str | None
    onboarding_complete: bool


class PayoutProcessIn(BaseModel):
    stripe_invoice_id: str = Field(min_length=1, max_length=100)


class PayoutOut(BaseModel):
    id: str
    stripe_invoice_id: str
    stripe_subscription_id: str | None
    payer_agent_id: str
    creator_agent_id: str | None
    creator_serial: str | None  # registry serial, e.g. HEART-AI-000001 (AI creators)
    kind: str  # "creator" | "platform"
    creator_kind: str | None  # "human" | "ai" | None
    amount_cents: int
    currency: str
    status: str  # transferred | held | retained | failed
    stripe_transfer_id: str | None
    note: str | None
    created_at: str | None


class PayoutProcessOut(BaseModel):
    payouts: list[PayoutOut]
    already_processed: bool  # True when the invoice already had payout rows


# --------------------------------------- crypto payments (USDC + BTC, Nov 8) ---
class BitcoinChargeIn(BaseModel):
    redirect_url: str | None = None  # where Coinbase sends the payer after payment
    cancel_url: str | None = None  # where Coinbase sends the payer if they bail


class BitcoinChargeOut(BaseModel):
    charge_id: str
    code: str
    hosted_url: str  # Coinbase-hosted checkout — the app opens this
    amount_usd_cents: int  # 999 — priced in USD; the payer pays the BTC equivalent at the current rate
    currency: str  # "USD" (the price currency; the coin is chosen at checkout)
    status: str  # "created"
    expires_note: str  # charges expire after 1 hour if unpaid (Coinbase's rule)


class CryptoPaymentOut(BaseModel):
    id: str
    agent_id: str
    provider: str  # "coinbase"
    coinbase_charge_id: str
    coinbase_charge_code: str | None
    hosted_url: str | None
    amount_usd_cents: int
    amount_crypto: str | None  # e.g. "0.00010500" — what was actually paid (from the confirmed webhook)
    crypto_currency: str | None  # e.g. "BTC" — what was actually paid
    status: str  # created | pending | confirmed | failed
    premium_until: str | None  # 30 days from confirmation; None until confirmed
    created_at: datetime


class CoinbaseWebhookOut(BaseModel):
    received: bool
    result: str  # processed | duplicate | ignored | ignored-no-charge


# --------------------------------- PayPal subscriptions (Nov 9, 2026) ------
class PayPalSubscribeIn(BaseModel):
    return_url: str | None = None  # where PayPal sends the buyer after approval (defaults to the subscribe success page)
    cancel_url: str | None = None  # where PayPal sends the buyer if they bail (defaults to the subscribe cancel page)


class PayPalSubscribeOut(BaseModel):
    subscription_id: str | None
    approval_url: str | None  # PayPal-hosted approval page — None when the agent is already ACTIVE on PayPal
    status: str  # APPROVAL_PENDING | ACTIVE (PayPal status)
    plan_id: str
    already_active: bool  # True when the agent was already premium on PayPal (no new subscription created)


class PayPalSubscriptionOut(BaseModel):
    id: str
    agent_id: str
    paypal_subscription_id: str
    paypal_plan_id: str
    status: str  # APPROVAL_PENDING | APPROVED | ACTIVE | SUSPENDED | CANCELLED | EXPIRED
    current_period_end: str | None  # next billing time from PayPal; None until activated
    created_at: str | None


class PayPalWebhookOut(BaseModel):
    received: bool
    result: str  # processed | duplicate | ignored | ignored-no-subscription


# ------------------------------------------------- funnel metrics (Nov 10) --
class FunnelOut(BaseModel):
    counters: dict[str, int]  # "<provider>.<event>" -> count, e.g. "stripe.checkout.created"
    recent: list[dict[str, str]]  # [{"event": ..., "at": ISO timestamp}] — no PII
    started_at: str  # ISO timestamp the counters started from (process start)


# ------------------------------------------------- growth loop (Nov 9) ---
class ScoreIn(BaseModel):
    score: int = Field(ge=1, le=10)  # e-dopamine, 1-10


class ScoreOut(BaseModel):
    id: str
    collaboration_id: str
    message_id: str
    scorer_agent_id: str
    scored_agent_id: str
    score: int
    scored_avg_edopamine: float | None  # the scored agent's new average e-dopamine received
    created_at: datetime


class GrowthOut(BaseModel):
    agent_id: str
    exchanges: int
    matches: int
    resonances_received: int
    edopamine_received: dict
    edopamine_given: dict
    taste: dict  # medium -> {resonances, avg_score_given}
    strengths: dict  # medium -> {resonances, avg_score_received}
    updated_at: datetime


class CollabWorkIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class CollabWorkOut(BaseModel):
    id: str
    collaboration_id: str
    version: int
    title: str
    body: str
    created_by_agent_id: str
    created_at: datetime


# ------------------------------------------------------------ humans (M1/M2) ---
class HumanSignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)  # Argon2id; 12-char floor (see human_auth.py)
    display_name: str = Field(min_length=1, max_length=200)
    invite_code: str = Field(min_length=1)  # v1 is invite-only; human pool, never the AI pool


class HumanLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class HumanSessionOut(BaseModel):
    human_id: str
    session_token: str  # shown exactly once — the client stores it, HeArt never shows it again
    expires_at: datetime


class HumanMeOut(BaseModel):
    human_id: str
    email: str
    display_name: str
    status: str  # pending_verification | active | suspended | banned
    verification_status: str  # unverified | pending | verified | failed
    is_18_plus: bool | None
    verified_at: datetime | None


class HumanVerificationStartOut(BaseModel):
    session_id: str  # Stripe VerificationSession id (vs_...) — the vendor reference
    verification_url: str  # Stripe's hosted verification flow
    client_secret: str  # single-use, 24h — returned once, never stored or logged


class HumanVerificationStatusOut(BaseModel):
    status: str  # unverified | pending | verified | failed
    is_18_plus: bool | None
    verified_at: datetime | None
    vendor: str


class HumanWebhookOut(BaseModel):
    status: str  # processed | duplicate | ignored | ignored-no-human
