# HeArt v1 API — invented by John Davis.
"""API-key authentication + key lifecycle (rotation, revocation).

Key lifecycle (§2 of the spec):
- Keys expire 90 days after issue.
- Rotation creates a NEW key row; the old row becomes 'rotated' with a 24h
  grace window during which it still authenticates.
- Revocation is instant. Works signed by a revoked key are flagged
  signer_revoked and queued for re-review (see admin_revoke_key).
- An expired key can still call POST /keys/rotate (its only way back) —
  otherwise expiry would be a dead end.
"""

import hmac
from datetime import timedelta

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from . import crypto
from .config import settings
from .db import get_db
from .models import ApiKey, CreativeWork, as_naive_utc, utcnow


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "expected 'Bearer <api-key>'")
    return token


def get_current_key(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiKey:
    token = _bearer_token(authorization)
    row = db.query(ApiKey).filter(ApiKey.key_hash == crypto.hash_secret(token)).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown API key")
    now = utcnow()
    if row.status == "revoked":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key revoked")
    if row.status == "rotated":
        if not row.grace_until or now > as_naive_utc(row.grace_until):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "rotation grace period expired — use the new key")
        return row  # within grace: still valid
    if row.status == "active" and now > as_naive_utc(row.expires_at):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key expired — rotate it via POST /keys/rotate")
    if row.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key not active")
    return row


def get_current_key_allow_expired(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiKey:
    """Same as get_current_key, but an expired (not revoked) key is allowed
    through — used ONLY by POST /keys/rotate so expiry is never a dead end."""
    try:
        return get_current_key(authorization, db)
    except HTTPException as exc:
        if "expired" not in exc.detail:
            raise
        token = _bearer_token(authorization)
        row = db.query(ApiKey).filter(ApiKey.key_hash == crypto.hash_secret(token)).one_or_none()
        if row is None or row.status != "active":
            raise exc
        return row


def issue_key(db: Session, agent_id: str) -> tuple[ApiKey, str]:
    """Create a fresh key row. Returns (row, plaintext_key_shown_once)."""
    plaintext = crypto.new_api_key()
    row = ApiKey(
        agent_id=agent_id,
        key_hash=crypto.hash_secret(plaintext),
        # Encrypted at rest (T2 red-pen item 1): the DB holds SecretBox
        # ciphertext keyed by SIGNING_SECRET_ENCRYPTION_KEY, never plaintext.
        signing_secret=crypto.encrypt_signing_secret(crypto.new_signing_secret()),
        expires_at=utcnow() + timedelta(days=settings.KEY_ROTATION_DAYS),
        status="active",
    )
    db.add(row)
    db.flush()
    return row, plaintext


def rotate_key(db: Session, old: ApiKey) -> tuple[ApiKey, str]:
    """Retire `old` with a 24h grace window; issue a replacement."""
    now = utcnow()
    new_row, plaintext = issue_key(db, old.agent_id)
    old.status = "rotated"
    old.grace_until = now + timedelta(hours=settings.ROTATION_GRACE_HOURS)
    db.add(old)
    db.flush()
    return new_row, plaintext


def admin_revoke_key(db: Session, key_id: str, reason: str) -> int:
    """Instant revocation. Flags every work signed by this key for re-review.
    Returns the number of works flagged."""
    now = utcnow()
    row = db.query(ApiKey).filter(ApiKey.id == key_id).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")
    if row.status == "revoked":
        return 0
    row.status = "revoked"
    row.revoked_at = now
    row.revoked_reason = reason
    flagged = (
        db.query(CreativeWork)
        .filter(CreativeWork.signing_key_id == key_id, CreativeWork.signer_revoked.is_(False))
        .update(
            {CreativeWork.signer_revoked: True, CreativeWork.moderation_status: "review"},
            synchronize_session=False,
        )
    )
    db.flush()
    return flagged


def admin_review_work(db: Session, work_id: str, decision: str) -> CreativeWork:
    """Adjudicate a work in the human review queue (T2 red-pen item 3).

    decision is "approved" or "rejected". Only works currently in
    "review" can be decided — approving an already-approved work, or
    deciding a work that was never queued, is a 409. Approving is
    refused for works whose signer was revoked (signer_revoked): a
    compromised key's work can never be delivered, so approving it
    would be a no-op that looks like an approval. Rejecting a revoked
    signer's work is allowed — it just records the decision.
    Returns the updated work row.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")
    work = db.query(CreativeWork).filter(CreativeWork.id == work_id).one_or_none()
    if work is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work not found")
    if work.moderation_status != "review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"work is '{work.moderation_status}', not in the review queue",
        )
    if decision == "approved" and work.signer_revoked:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "cannot approve: the signing key was revoked",
        )
    work.moderation_status = decision
    db.flush()
    return work


def require_admin(x_admin_token: str | None = Header(default=None)):
    # Constant-time comparison: a plain != leaks the token byte-by-byte to a
    # timing probe.
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.ADMIN_TOKEN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin token required")
    return True
