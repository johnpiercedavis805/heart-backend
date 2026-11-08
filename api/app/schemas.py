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


# ------------------------------------------------ Stripe billing (HeArt Premium, Nov 8) ---
class CheckoutOut(BaseModel):
    checkout_url: str
    session_id: str


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
    stripe_price_id: str | None
    current_period_end: str | None
    cancel_at_period_end: bool


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
