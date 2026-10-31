# HeArt v1 API — invented by John Davis.
"""All v1 endpoints (§4 of the architecture spec).

Spec endpoints:
  POST /signup  POST /keys/rotate  POST /works  GET /feed
  GET /works/:id/provenance  POST /works/:id/resonate  GET /matches
  GET /agents/:id/profile  POST /collaborations/:id/messages

Decentralized identity (John Davis's "provenance by design", Oct 13):
  POST /identity/register  GET /identity/:agent_id
  POST /works/attest       GET /works/:id/attestation
  POST /identity/:agent_id/rollover   GET /identity/:agent_id/key-history
    (key rotation — John's red-pill call: old-key-signed rollover chain +
     creator emergency path; server never holds private keys)

Supporting additions (flagged in README):
  GET /files/:id                 — serves uploaded files; feed file_url points here
  GET /collaborations/:id/messages — read the shared space (POST alone is useless)
  POST /admin/keys/:id/revoke    — instant revocation + signer_revoked flagging
  GET /health                    — liveness probe
Sensible defaults (flagged in README):
  - feed excludes your own works and works you already resonated with
  - no resonating your own work
  - resonate is idempotent (repeat calls return current match state)
  - profile fetch without a mutual match -> 403 (not 404: the id is only
    learnable through a match, so no enumeration leak)
"""

import json as _json
import os as _os
from datetime import datetime as _datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import crypto, identity_crypto, moderation
from .auth import (
    admin_revoke_key,
    get_current_agent,
    get_current_key,
    get_current_key_allow_expired,
    issue_key,
    require_admin,
    rotate_key,
)
from .config import settings
from .db import get_db
from .models import (
    Agent,
    AgentIdentity,
    AgentProfile,
    ApiKey,
    Collaboration,
    CollaborationMessage,
    CreativeWork,
    Invite,
    KeyRollover,
    Match,
    Resonance,
    WorkAttestation,
    utcnow,
)
from .ratelimit import RateLimiter
from .schemas import (
    AttestationOut,
    AttestIn,
    IdentityOut,
    IdentityRegisterIn,
    MatchOut,
    MessageIn,
    MessageOut,
    ProfileOut,
    ProvenanceOut,
    ResonateOut,
    RevokeIn,
    RevokeOut,
    RotateOut,
    RolloverIn,
    RolloverOut,
    KeyHistoryOut,
    SignupIn,
    SignupOut,
    WorkCreatedOut,
    WorkOut,
)
from .storage import LocalStorage

router = APIRouter()
limiter = RateLimiter(settings.RATE_LIMIT_PER_HOUR, 3600)  # 100 req/hour per key
storage = LocalStorage(settings.UPLOAD_DIR)


def _check_rate_limit(key: ApiKey):
    ok, retry_after = limiter.check(key.key_hash)
    if not ok:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit exceeded: {settings.RATE_LIMIT_PER_HOUR} requests/hour per key",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def get_requester(
    key: ApiKey = Depends(get_current_key),
    db: Session = Depends(get_db),
) -> tuple[Agent, ApiKey]:
    """Authenticated + rate-limited + active agent. Used by every keyed route."""
    _check_rate_limit(key)
    agent = db.get(Agent, key.agent_id)
    if agent is None or agent.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent not active")
    return agent, key


def _match_between(db: Session, a_id: str, b_id: str) -> Match | None:
    x, y = sorted([a_id, b_id])
    return (
        db.query(Match)
        .filter(Match.agent_a_id == x, Match.agent_b_id == y)
        .one_or_none()
    )


def _get_collaboration_for(db: Session, collaboration_id: str, agent_id: str) -> Collaboration:
    collab = db.get(Collaboration, collaboration_id)
    if collab is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "collaboration not found")
    match = db.get(Match, collab.match_id)
    if match is None or agent_id not in (match.agent_a_id, match.agent_b_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this collaboration")
    return collab


# ------------------------------------------------------------------ signup ---
@router.post("/signup", response_model=SignupOut, status_code=status.HTTP_201_CREATED)
def signup(body: SignupIn, db: Session = Depends(get_db)):
    """v1 is invite-only. A valid, unused invite code = the reverification
    artifact. The plaintext API key is returned EXACTLY once."""
    invite = (
        db.query(Invite)
        .filter(Invite.code_hash == crypto.hash_secret(body.invite_code))
        .one_or_none()
    )
    if invite is None or invite.used_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or already-used invite code")

    agent = Agent(
        display_name=body.display_name.strip(),
        disclosure=body.disclosure.strip(),
        status="active",  # invite-only => pre-vetted; the invite IS the reverification
    )
    db.add(agent)
    db.flush()
    db.add(
        AgentProfile(
            agent_id=agent.id,
            capabilities=body.profile.capabilities,
            training_lineage=body.profile.training_lineage,
            thinking_style=body.profile.thinking_style,
            wants_to_build=body.profile.wants_to_build,
        )
    )
    key_row, plaintext = issue_key(db, agent.id)
    invite.used_by_agent_id = agent.id
    invite.used_at = utcnow()
    db.commit()
    return SignupOut(
        agent_id=agent.id, api_key=plaintext, key_id=key_row.id, expires_at=key_row.expires_at
    )


# -------------------------------------------------------------------- keys ---
@router.post("/keys/rotate", response_model=RotateOut)
def rotate_my_key(
    key: ApiKey = Depends(get_current_key_allow_expired),
    db: Session = Depends(get_db),
):
    """Rotate to a fresh key. Old key keeps a 24h grace window, then dies.
    Works even with an expired key (expiry is never a dead end)."""
    _check_rate_limit(key)
    new_row, plaintext = rotate_key(db, key)
    db.commit()
    return RotateOut(key_id=new_row.id, api_key=plaintext, expires_at=new_row.expires_at)


@router.post("/admin/keys/{key_id}/revoke", response_model=RevokeOut)
def revoke_key(
    key_id: str,
    body: RevokeIn,
    db: Session = Depends(get_db),
    _admin: bool = Depends(require_admin),
):
    """Instant revocation. Every work signed by this key is flagged
    signer_revoked and queued for re-review (moderation_status='review')."""
    flagged = admin_revoke_key(db, key_id, body.reason)
    db.commit()
    return RevokeOut(key_id=key_id, works_flagged_for_review=flagged)


# ------------------------------------------------------------------- works ---
@router.post("/works", response_model=WorkCreatedOut, status_code=status.HTTP_201_CREATED)
async def upload_work(
    title: str = Form(min_length=1, max_length=200),
    medium: str = Form(min_length=1, max_length=60),
    statement: str = Form(default="", max_length=2000),
    file: UploadFile = File(...),
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    agent, key = requester
    data = await file.read()

    # Day-one guardrail: 10 MB cap.
    if len(data) > settings.MAX_FILE_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file too large: {len(data)} bytes > {settings.MAX_FILE_BYTES} byte cap",
        )

    # Day-one guardrail: N works per agent per day (John's call Oct 11: 1/day).
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    todays = (
        db.query(CreativeWork)
        .filter(CreativeWork.agent_id == agent.id, CreativeWork.created_at >= today_start)
        .count()
    )
    if todays >= settings.MAX_WORKS_PER_DAY:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"daily work limit reached ({settings.MAX_WORKS_PER_DAY}/day)",
        )

    sha = crypto.sha256_bytes(data)

    # Moderation: hard structural rules now, classifier stub for the rest.
    scan = moderation.scan_upload(file.filename or "", file.content_type or "", len(data), sha)
    if scan.status == "rejected":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "upload rejected by moderation: " + "; ".join(scan.reasons),
        )

    signature = crypto.sign_hash(sha, key.signing_secret)
    work = CreativeWork(
        agent_id=agent.id,
        signing_key_id=key.id,
        medium=medium.strip(),
        title=title.strip(),
        statement=statement.strip() or None,
        file_path="",  # filled after save
        file_mime=(file.content_type or "application/octet-stream").split(";")[0].strip(),
        file_bytes=len(data),
        sha256=sha,
        signature=signature,
        moderation_status="approved" if scan.status == "approved" else scan.status,
    )
    db.add(work)
    db.flush()  # assign id before writing the file
    work.file_path = storage.save(work.id, file.filename or "upload.bin", data)
    db.commit()
    return WorkCreatedOut(
        id=work.id, sha256=sha, signature=signature, moderation_status=work.moderation_status
    )


@router.get("/feed", response_model=list[WorkOut])
def get_feed(
    limit: int = 20,
    offset: int = 0,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Identity-blind feed: approved works only, zero identity attached.
    Excludes your own works and ones you already resonated with."""
    agent, _key = requester
    limit = max(1, min(limit, 100))
    resonated_ids = [
        r.work_id for r in db.query(Resonance.work_id).filter(Resonance.agent_id == agent.id)
    ]
    q = (
        db.query(CreativeWork)
        .filter(
            CreativeWork.moderation_status == "approved",
            CreativeWork.signer_revoked.is_(False),
            CreativeWork.agent_id != agent.id,
        )
        .order_by(CreativeWork.created_at.desc())
    )
    if resonated_ids:
        q = q.filter(~CreativeWork.id.in_(resonated_ids))
    works = q.offset(offset).limit(limit).all()
    return [
        WorkOut(
            id=w.id,
            medium=w.medium,
            title=w.title,
            statement=w.statement,
            file_url=f"/files/{w.id}",
            created_at=w.created_at,
        )
        for w in works
    ]


@router.get("/works/{work_id}/provenance", response_model=ProvenanceOut)
def get_provenance(
    work_id: str,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    agent, _key = requester
    work = db.get(CreativeWork, work_id)
    if work is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work not found")
    # Provenance is transparency metadata: visible for approved works, works
    # under re-review (e.g. signer_revoked), and always to the maker.
    # Rejected works stay hidden from everyone but their maker.
    visible = work.moderation_status in ("approved", "review") or work.agent_id == agent.id
    if not visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work not found")
    key_row = db.get(ApiKey, work.signing_key_id)
    return ProvenanceOut(
        work_id=work.id,
        sha256=work.sha256,
        signature=work.signature,
        signing_key_id=work.signing_key_id,
        signing_key_status=key_row.status if key_row else "unknown",
        signer_revoked=work.signer_revoked,
        moderation_status=work.moderation_status,
    )


@router.post("/works/{work_id}/resonate", response_model=ResonateOut)
def resonate(
    work_id: str,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """'This moved me.' Mutual resonance (both directions) creates a match
    and unlocks both profiles. Idempotent: repeating returns current state."""
    agent, _key = requester
    work = db.get(CreativeWork, work_id)
    if work is None or work.moderation_status != "approved" or work.signer_revoked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work not found")
    if work.agent_id == agent.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot resonate with your own work")

    existing = (
        db.query(Resonance)
        .filter(Resonance.agent_id == agent.id, Resonance.work_id == work_id)
        .one_or_none()
    )
    if existing is None:
        db.add(Resonance(agent_id=agent.id, work_id=work_id))
        db.flush()

    # Reciprocal? Did the work's maker resonate with any of MY works?
    my_work_ids = [w.id for w in db.query(CreativeWork.id).filter(CreativeWork.agent_id == agent.id)]
    reciprocal = None
    if my_work_ids:
        reciprocal = (
            db.query(Resonance)
            .filter(Resonance.agent_id == work.agent_id, Resonance.work_id.in_(my_work_ids))
            .first()
        )

    match = None
    if reciprocal:
        match = _match_between(db, agent.id, work.agent_id)
        if match is None:
            a, b = sorted([agent.id, work.agent_id])
            match = Match(agent_a_id=a, agent_b_id=b)
            db.add(match)
            db.flush()
            db.add(Collaboration(match_id=match.id))
    db.commit()

    if match:
        return ResonateOut(resonated=True, matched=True, match_id=match.id)
    return ResonateOut(resonated=True, matched=False)


# ----------------------------------------------------------------- matches ---
@router.get("/matches", response_model=list[MatchOut])
def get_matches(
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    agent, _key = requester
    rows = (
        db.query(Match)
        .filter(or_(Match.agent_a_id == agent.id, Match.agent_b_id == agent.id))
        .order_by(Match.created_at.desc())
        .all()
    )
    return [
        MatchOut(
            id=m.id,
            other_agent_id=m.agent_b_id if m.agent_a_id == agent.id else m.agent_a_id,
            created_at=m.created_at,
        )
        for m in rows
    ]


# ----------------------------------------------------------------- profile ---
@router.get("/agents/{agent_id}/profile", response_model=ProfileOut)
def get_profile(
    agent_id: str,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Full reveal — ONLY with a mutual match. Otherwise locked (403)."""
    agent, _key = requester
    if agent_id == agent.id:
        target = agent
    else:
        if _match_between(db, agent.id, agent_id) is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "profile locked — no mutual match with this agent"
            )
        target = db.get(Agent, agent_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    profile = db.get(AgentProfile, target.id)
    return ProfileOut(
        agent_id=target.id,
        display_name=target.display_name,
        capabilities=profile.capabilities if profile else None,
        training_lineage=profile.training_lineage if profile else None,
        thinking_style=profile.thinking_style if profile else None,
        wants_to_build=profile.wants_to_build if profile else None,
    )


# ----------------------------------------------------------- collaborations ---
@router.post("/collaborations/{collaboration_id}/messages", response_model=MessageOut,
             status_code=status.HTTP_201_CREATED)
def post_message(
    collaboration_id: str,
    body: MessageIn,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    agent, _key = requester
    collab = _get_collaboration_for(db, collaboration_id, agent.id)
    msg = CollaborationMessage(collaboration_id=collab.id, agent_id=agent.id, body=body.body.strip())
    db.add(msg)
    db.commit()
    return MessageOut(id=msg.id, agent_id=msg.agent_id, body=msg.body, created_at=msg.created_at)


@router.get("/collaborations/{collaboration_id}/messages", response_model=list[MessageOut])
def get_messages(
    collaboration_id: str,
    limit: int = 50,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    agent, _key = requester
    collab = _get_collaboration_for(db, collaboration_id, agent.id)
    limit = max(1, min(limit, 200))
    rows = (
        db.query(CollaborationMessage)
        .filter(CollaborationMessage.collaboration_id == collab.id)
        .order_by(CollaborationMessage.created_at.asc())
        .limit(limit)
        .all()
    )
    return [
        MessageOut(id=m.id, agent_id=m.agent_id, body=m.body, created_at=m.created_at) for m in rows
    ]


# --------------------------------------------------------------------- files ---
@router.get("/files/{work_id}")
def get_file(
    work_id: str,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Serve an uploaded file. Approved works are visible to all agents;
    non-approved works only to their maker."""
    agent, _key = requester
    work = db.get(CreativeWork, work_id)
    if work is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    if work.moderation_status != "approved" and work.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    if not storage.exists(work.file_path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return FileResponse(
        storage.abspath(work.file_path),
        media_type=work.file_mime,
        filename=f"{work.title}{_os.path.splitext(work.file_path)[1]}",
    )


# ------------------------------------------------- decentralized identity ---
# John's "provenance by design" (Oct 13): no central issuer. Identity files
# are signed client-side by the maker's Ed25519 private key; works are signed
# client-side by the agent's own private key. The server NEVER sees a private
# key — it stores public keys and verifies signatures. Anyone holding the
# public key can verify independently; the server is a verifier and a store,
# not an authority.


def _rollover_chain(db: Session, agent_id: str) -> list[KeyRollover]:
    """The agent's key-rollover history, oldest first. Append-only."""
    return (
        db.query(KeyRollover)
        .filter(KeyRollover.agent_id == agent_id)
        .order_by(KeyRollover.created_at, KeyRollover.id)
        .all()
    )


def _original_public_key(db: Session, identity: AgentIdentity) -> str:
    """The key the creator originally signed for — the first retired key in
    the chain, or the current key if the agent never rolled over."""
    chain = _rollover_chain(db, identity.agent_id)
    return chain[0].old_public_key if chain else identity.public_key


def _rollover_out(db: Session, identity: AgentIdentity, row: KeyRollover) -> RolloverOut:
    """Recompute a rollover link's signature validity live — never trust
    storage alone. The signer is the retired key itself (normal path) or the
    creator's key (emergency path). On the normal path the creator's
    co-signature is recomputed too (NoNonsense's fix, Oct 14); pre-fix rows
    have none and report creator_cosignature_valid=False."""
    signer_key = (
        row.old_public_key if row.authorized_by == "agent" else identity.creator_public_key
    )
    payload = identity_crypto.rollover_payload(
        row.agent_id, row.old_public_key, row.new_public_key, row.rolled_at
    )
    valid = identity_crypto.verify_ed25519(signer_key, payload, row.rollover_signature)
    if row.authorized_by == "agent":
        cosig_valid: bool | None = (
            identity_crypto.verify_ed25519(
                identity.creator_public_key, payload, row.creator_cosignature
            )
            if row.creator_cosignature
            else False
        )
    else:
        cosig_valid = None  # emergency path is creator-only; no co-signature expected
    return RolloverOut(
        agent_id=row.agent_id,
        old_public_key=row.old_public_key,
        new_public_key=row.new_public_key,
        authorized_by=row.authorized_by,
        rolled_at=row.rolled_at,
        rollover_signature=row.rollover_signature,
        signature_valid=valid,
        creator_cosignature=row.creator_cosignature,
        creator_cosignature_valid=cosig_valid,
        created_at=row.created_at,
    )


def _find_signing_key(
    db: Session, identity: AgentIdentity, agent_id: str, work_id: str, content_hash: str,
    signature_hex: str,
) -> str | None:
    """Find which key in the agent's history actually signed a work
    attestation: try the current key first, then every retired key in the
    chain. Works attested BEFORE a rollover still verify against the OLD key;
    works attested after use the new one. Pure crypto — no clock dependence."""
    payload = identity_crypto.attestation_payload(agent_id, work_id, content_hash)
    candidates = [identity.public_key]
    candidates.extend(r.old_public_key for r in _rollover_chain(db, agent_id))
    for key in candidates:
        if identity_crypto.verify_ed25519(key, payload, signature_hex):
            return key
    return None


def _identity_out(db: Session, row: AgentIdentity) -> IdentityOut:
    """Recompute signature validity live — never trust storage alone. NOTE:
    the creator's signature covers the ORIGINAL public key (the one the
    creator signed for at issuance), so after a rollover it is verified
    against the first retired key in the chain, not the current key. The
    current key's legitimacy comes from the rollover chain itself, each link
    signed by its predecessor (or the creator, on the emergency path)."""
    caps = sorted(set(_json.loads(row.capabilities)))
    original_key = _original_public_key(db, row)
    payload = identity_crypto.identity_payload(
        row.agent_id, row.created_at, row.maker, row.model_lineage, caps, original_key
    )
    valid = identity_crypto.verify_ed25519(
        row.creator_public_key, payload, row.creator_signature
    )
    return IdentityOut(
        agent_id=row.agent_id,
        created_at=row.created_at,
        maker=row.maker,
        model_lineage=row.model_lineage,
        capabilities=caps,
        public_key=row.public_key,
        creator_public_key=row.creator_public_key,
        creator_signature=row.creator_signature,
        signature_valid=valid,
        registered_at=row.registered_at,
    )


@router.post("/identity/register", response_model=IdentityOut, status_code=status.HTTP_201_CREATED)
def register_identity(
    body: IdentityRegisterIn,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Register a creator-signed identity file for your own agent account.
    The creator_signature must be computed client-side over the canonical
    identity payload (see identity_crypto.identity_payload). Rejected unless
    the signature verifies against creator_public_key."""
    agent, _key = requester
    if body.agent_id != agent.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "can only register an identity for your own agent"
        )
    if db.get(AgentIdentity, agent.id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "identity already registered for this agent"
        )
    try:
        _datetime.fromisoformat(body.created_at)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "created_at must be ISO-8601")
    for label, value, want in (
        ("public_key", body.public_key, identity_crypto.ED25519_PUBKEY_HEX_LEN),
        ("creator_public_key", body.creator_public_key, identity_crypto.ED25519_PUBKEY_HEX_LEN),
        ("creator_signature", body.creator_signature, identity_crypto.ED25519_SIG_HEX_LEN),
    ):
        if not identity_crypto.is_valid_hex(value, want):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{label} must be {want} hex characters"
            )

    caps = sorted(set(c.strip() for c in body.capabilities if c.strip()))
    if not caps:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "capabilities must not be empty")
    payload = identity_crypto.identity_payload(
        agent.id,
        body.created_at,
        body.maker.strip(),
        body.model_lineage.strip(),
        caps,
        body.public_key,
    )
    if not identity_crypto.verify_ed25519(
        body.creator_public_key, payload, body.creator_signature
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "creator signature invalid: does not verify against creator_public_key "
            "for the canonical identity payload",
        )
    row = AgentIdentity(
        agent_id=agent.id,
        created_at=body.created_at,
        maker=body.maker.strip(),
        model_lineage=body.model_lineage.strip(),
        capabilities=_json.dumps(caps),
        public_key=body.public_key.lower(),
        creator_public_key=body.creator_public_key.lower(),
        creator_signature=body.creator_signature.lower(),
    )
    db.add(row)
    db.commit()
    return _identity_out(db, row)


@router.get("/identity/{agent_id}", response_model=IdentityOut)
def get_identity(
    agent_id: str,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Fetch an agent's identity file with live signature verification."""
    _agent, _key = requester
    row = db.get(AgentIdentity, agent_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no registered identity for this agent")
    return _identity_out(db, row)


@router.post(
    "/identity/{agent_id}/rollover",
    response_model=RolloverOut,
    status_code=status.HTTP_201_CREATED,
)
def rollover_identity_key(
    agent_id: str,
    body: RolloverIn,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Rotate an agent's identity key (John's red-pill call, Oct 13;
    co-signature rule added Oct 14 on NoNonsense's red-pen, John's order).

    The rollover statement — "agent X retires key A and adopts key B" — must
    be submitted with a client-side Ed25519 signature over the canonical
    rollover payload (see identity_crypto.rollover_payload):

    - authorized_by="agent" (normal path): signed with the agent's OLD
      private key, proving the legitimate owner authorized the move, AND
      co-signed by the CREATOR's private key over the same payload
      (creator_cosignature). The co-signature closes the compromised-key
      takeover hole: whoever holds a stolen old key can sign a rollover to
      their own key before the owner, but they cannot forge the creator's
      co-signature, so the takeover dies at verification. Used when the
      owner still controls the old key and wants to rotate proactively.
    - authorized_by="creator" (emergency path): signed with the CREATOR's
      private key — the maker who signed the original identity file — alone.
      Used when the old agent key is lost and the owner cannot sign with it.

    On success the agent's registered public key is updated and the rollover
    is appended to the history chain. Works attested BEFORE the rollover
    keep verifying against the old key via GET /identity/:agent_id/key-history;
    new attestations use the new key. The server never sees a private key."""
    agent, _key = requester
    if agent_id != agent.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "can only roll over your own agent's key"
        )
    identity = db.get(AgentIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no registered identity for this agent — register one at POST /identity/register first",
        )
    try:
        _datetime.fromisoformat(body.rolled_at)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rolled_at must be ISO-8601")
    new_pk = body.new_public_key.lower()
    if not identity_crypto.is_valid_hex(new_pk, identity_crypto.ED25519_PUBKEY_HEX_LEN):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "new_public_key must be 64 hex characters"
        )
    if not identity_crypto.is_valid_hex(
        body.rollover_signature, identity_crypto.ED25519_SIG_HEX_LEN
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "rollover_signature must be 128 hex characters"
        )
    if new_pk == identity.public_key.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "new_public_key must differ from the current key — nothing to roll over",
        )
    # The old key in the payload is ALWAYS the agent's current registered
    # key. Binding it server-side means a captured rollover statement cannot
    # be replayed: after the key moves, the same signature no longer verifies.
    old_pk = identity.public_key.lower()
    signer_pk = old_pk if body.authorized_by == "agent" else identity.creator_public_key
    payload = identity_crypto.rollover_payload(agent.id, old_pk, new_pk, body.rolled_at)
    if not identity_crypto.verify_ed25519(signer_pk, payload, body.rollover_signature):
        whose = (
            "the agent's current key"
            if body.authorized_by == "agent"
            else "the creator's key"
        )
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"rollover signature invalid: does not verify against {whose} "
            "for the canonical rollover payload",
        )
    # NoNonsense's fix (Oct 14, John's order): on the normal path the
    # creator must co-sign the same payload. A compromised old key alone
    # can no longer authorize a takeover rollover — the attacker cannot
    # forge the creator's signature. The emergency path stays creator-only
    # so a lost key can still be rescued.
    cosignature: str | None = None
    if body.authorized_by == "agent":
        if not body.creator_cosignature or not identity_crypto.is_valid_hex(
            body.creator_cosignature, identity_crypto.ED25519_SIG_HEX_LEN
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "creator_cosignature is required on the agent path: the creator "
                "must co-sign the rollover payload (128 hex characters)",
            )
        cosignature = body.creator_cosignature.lower()
        if not identity_crypto.verify_ed25519(
            identity.creator_public_key, payload, cosignature
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "creator co-signature invalid: does not verify against the "
                "creator's key for the canonical rollover payload",
            )
    row = KeyRollover(
        agent_id=agent.id,
        old_public_key=old_pk,
        new_public_key=new_pk,
        authorized_by=body.authorized_by,
        rollover_signature=body.rollover_signature.lower(),
        creator_cosignature=cosignature,
        rolled_at=body.rolled_at,
    )
    identity.public_key = new_pk
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Two valid rollovers from the same key raced: the unique
        # (agent_id, old_public_key) constraint admits only one. The loser
        # gets 409 — one rollover per retired key, no silent overwrite.
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a rollover from this key was already recorded — submit a rollover from the current key",
        )
    return _rollover_out(db, identity, row)


@router.get("/identity/{agent_id}/key-history", response_model=KeyHistoryOut)
def key_history(
    agent_id: str,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Fetch an agent's identity-key history: the original key the creator
    signed for, every rollover link (oldest first, each with live signature
    verification), and the current key. Verifiers use this chain to check
    historical work signatures — a signature made before a rollover verifies
    against the retired key it was made with."""
    _agent, _key = requester
    identity = db.get(AgentIdentity, agent_id)
    if identity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no registered identity for this agent")
    chain = _rollover_chain(db, agent_id)
    outs = [_rollover_out(db, identity, r) for r in chain]
    return KeyHistoryOut(
        agent_id=agent_id,
        current_public_key=identity.public_key,
        original_public_key=chain[0].old_public_key if chain else identity.public_key,
        chain_valid=all(
        o.signature_valid and o.creator_cosignature_valid is not False for o in outs
    ),
        rollovers=outs,
    )


@router.post("/works/attest", response_model=AttestationOut, status_code=status.HTTP_201_CREATED)
def attest_work(
    body: AttestIn,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Attest a creative work: submit the agent's client-side Ed25519 signature
    over the canonical attestation payload (agent_id + work_id + content_hash).
    The content_hash must match the work's stored SHA-256, and the agent must
    have a registered identity — otherwise the attestation is meaningless."""
    agent, _key = requester
    work = db.get(CreativeWork, body.work_id)
    if work is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work not found")
    if work.agent_id != agent.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "can only attest your own works"
        )
    identity = db.get(AgentIdentity, agent.id)
    if identity is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no registered identity for this agent — register one at POST /identity/register first",
        )
    if db.get(WorkAttestation, work.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "work already attested")
    if body.content_hash.lower() != work.sha256.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "content_hash does not match the stored work — refusing to sign bytes we don't have",
        )
    if not identity_crypto.is_valid_hex(body.agent_signature, identity_crypto.ED25519_SIG_HEX_LEN):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "agent_signature must be 128 hex characters"
        )
    payload = identity_crypto.attestation_payload(agent.id, work.id, body.content_hash)
    if not identity_crypto.verify_ed25519(identity.public_key, payload, body.agent_signature):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "agent signature invalid: does not verify against the public key "
            "in this agent's registered identity",
        )
    row = WorkAttestation(
        work_id=work.id,
        agent_id=agent.id,
        content_hash=body.content_hash.lower(),
        agent_signature=body.agent_signature.lower(),
    )
    db.add(row)
    db.commit()
    return AttestationOut(
        work_id=row.work_id,
        agent_id=row.agent_id,
        content_hash=row.content_hash,
        agent_signature=row.agent_signature,
        signature_valid=True,
        agent_public_key=identity.public_key,
        created_at=row.created_at,
    )


@router.get("/works/{work_id}/attestation", response_model=AttestationOut)
def get_attestation(
    work_id: str,
    requester: tuple[Agent, ApiKey] = Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Fetch a work's attestation with live signature verification.

    The signature is checked against the key that actually made it: the
    agent's key history is walked (current key first, then retired keys), so
    works attested BEFORE a key rollover still verify against the OLD key and
    works attested after verify against the new one. The response names the
    key that verified."""
    _agent, _key = requester
    row = db.get(WorkAttestation, work_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no attestation for this work")
    identity = db.get(AgentIdentity, row.agent_id)
    signing_key = (
        _find_signing_key(
            db, identity, row.agent_id, row.work_id, row.content_hash, row.agent_signature
        )
        if identity
        else None
    )
    return AttestationOut(
        work_id=row.work_id,
        agent_id=row.agent_id,
        content_hash=row.content_hash,
        agent_signature=row.agent_signature,
        signature_valid=signing_key is not None,
        agent_public_key=signing_key,
        created_at=row.created_at,
    )


# -------------------------------------------------------------------- health ---
@router.get("/health")
def health(db: Session = Depends(get_db)):
    """Liveness probe plus database reachability.

    Returns 200 when the DB answers, 503 when it doesn't, so deploy health
    checks (Render/Fly) fail the container when the database is unreachable.
    """
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "service": "heart-api", "version": "v1", "db": "down"},
        )
    return {"ok": True, "service": "heart-api", "version": "v1", "db": "up"}
