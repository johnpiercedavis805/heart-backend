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
