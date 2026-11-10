# HeArt v1 API — invented by John Davis.
"""Human accounts + session auth (M1 of the human-to-human track).

Separate from the AI track on purpose: humans live in their own tables
(`humans`, `human_sessions`, `human_invites` — see models.py), with their
own tokens (`hs_...` prefix, never `hk_...`), and their own auth dependency.
A human token can never authenticate as an AI agent and vice versa — the
tokens live in different tables, and a hash lookup in the wrong table finds
nothing (the test suite asserts both directions).

Passwords: Argon2id (argon2-cffi `PasswordHasher` — Argon2id is its
default), the OWASP-recommended password hash. The PHC string
($argon2id$v=19$m=...,t=...,p=...$salt$hash) is all that is stored; the
plaintext is never stored, logged, or returned. Minimum password length is
12 (a policy choice, documented here — not John's call).

Sessions mirror the ApiKey lifecycle exactly (§2 of the spec, auth.py):
- Sessions expire 90 days after issue (KEY_ROTATION_DAYS).
- Rotation creates a NEW session row; the old row becomes 'rotated' with a
  24h grace window (ROTATION_GRACE_HOURS) during which it still works.
- Revocation is instant.
- An expired session can still call POST /humans/sessions/rotate (its only
  way back) — otherwise expiry would be a dead end.
"""

import secrets
from datetime import timedelta

import argon2
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from . import crypto
from .config import settings
from .db import get_db
from .models import Human, HumanSession, as_naive_utc, utcnow

SESSION_PREFIX = "hs_"
MIN_PASSWORD_LENGTH = 12

# Argon2id with argon2-cffi's defaults (time_cost=3, memory_cost=64 MiB,
# parallelism=4, hash_len=32, salt_len=16). The defaults are the OWASP
# recommendation for Argon2id; do not weaken them to shave login latency.
_password_hasher = argon2.PasswordHasher()


def hash_password(password: str) -> str:
    """Argon2id hash of a plaintext password. Returns the PHC string."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True when `password` matches the stored Argon2id hash. A wrong
    password, a malformed hash, and a hash for different parameters all
    return False — never raise, never leak which case it was."""
    try:
        return _password_hasher.verify(password_hash, password)
    except Exception:
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash was made with weaker parameters than the
    current hasher — the next successful login should re-hash it."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except Exception:
        return False


def new_human_session_token() -> str:
    return SESSION_PREFIX + secrets.token_urlsafe(32)


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "expected 'Bearer <session token>'")
    return token


def get_current_human(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Human:
    """The authenticated human for a `hs_...` session token. Mirrors
    auth.get_current_key: revoked is instant-dead, rotated works inside
    its 24h grace, expired is dead (use the rotate endpoint)."""
    token = _bearer_token(authorization)
    row = db.query(HumanSession).filter(HumanSession.key_hash == crypto.hash_secret(token)).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown session token")
    now = utcnow()
    if row.status == "revoked":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session revoked")
    if row.status == "rotated":
        if not row.grace_until or now > as_naive_utc(row.grace_until):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "rotation grace period expired — use the new session token",
            )
        return _human_or_401(db, row.human_id)
    if row.status == "active" and now > as_naive_utc(row.expires_at):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "session expired — rotate it via POST /humans/sessions/rotate",
        )
    if row.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session not active")
    return _human_or_401(db, row.human_id)


def get_current_human_allow_expired(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Human:
    """Same as get_current_human, but an expired (not revoked) session is
    allowed through — used ONLY by POST /humans/sessions/rotate so expiry
    is never a dead end."""
    try:
        return get_current_human(authorization, db)
    except HTTPException as exc:
        if "expired" not in exc.detail:
            raise
        token = _bearer_token(authorization)
        row = db.query(HumanSession).filter(HumanSession.key_hash == crypto.hash_secret(token)).one_or_none()
        if row is None or row.status != "active":
            raise exc
        return _human_or_401(db, row.human_id)


def _human_or_401(db: Session, human_id: str) -> Human:
    human = db.get(Human, human_id)
    if human is None:  # the account was deleted but the session row survived
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown session token")
    return human


def issue_session(db: Session, human_id: str) -> tuple[HumanSession, str]:
    """Create a fresh session row. Returns (row, plaintext_token_shown_once)."""
    plaintext = new_human_session_token()
    row = HumanSession(
        human_id=human_id,
        key_hash=crypto.hash_secret(plaintext),
        expires_at=utcnow() + timedelta(days=settings.KEY_ROTATION_DAYS),
        status="active",
    )
    db.add(row)
    db.flush()
    return row, plaintext


def rotate_session(db: Session, old: HumanSession) -> tuple[HumanSession, str]:
    """Retire `old` with a 24h grace window; issue a replacement."""
    now = utcnow()
    new_row, plaintext = issue_session(db, old.human_id)
    old.status = "rotated"
    old.grace_until = now + timedelta(hours=settings.ROTATION_GRACE_HOURS)
    db.add(old)
    db.flush()
    return new_row, plaintext


def revoke_session(db: Session, session_row: HumanSession, reason: str) -> None:
    """Instant revocation of one session row."""
    session_row.status = "revoked"
    session_row.revoked_at = utcnow()
    session_row.revoked_reason = reason
    db.add(session_row)
    db.flush()


def get_session_row_allow_expired(db: Session, authorization: str | None) -> HumanSession:
    """Same as get_session_row, but an expired (not revoked) session is
    allowed through — used ONLY by POST /humans/sessions/rotate so expiry
    is never a dead end."""
    try:
        return get_session_row(db, authorization)
    except HTTPException as exc:
        if "expired" not in exc.detail:
            raise
        token = _bearer_token(authorization)
        row = db.query(HumanSession).filter(HumanSession.key_hash == crypto.hash_secret(token)).one_or_none()
        if row is None or row.status != "active":
            raise exc
        return row


def get_session_row(db: Session, authorization: str | None) -> HumanSession:
    """The HumanSession row behind a bearer token (for logout/rotate,
    which need the row itself, not just the human). Raises 401 like
    get_current_human when the token is unknown, revoked, or expired."""
    token = _bearer_token(authorization)
    row = db.query(HumanSession).filter(HumanSession.key_hash == crypto.hash_secret(token)).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown session token")
    now = utcnow()
    if row.status == "revoked":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session revoked")
    if row.status == "rotated":
        if not row.grace_until or now > as_naive_utc(row.grace_until):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "rotation grace period expired — use the new session token",
            )
        return row
    if row.status == "active" and now > as_naive_utc(row.expires_at):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "session expired — rotate it via POST /humans/sessions/rotate",
        )
    if row.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session not active")
    return row
