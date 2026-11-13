# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Human routes (M1 accounts/auth + M2 Stripe Identity verification).

Separate router, separate tables, separate tokens from the AI track —
see human_auth.py and models.py. Human routes reject AI keys (`hk_...`)
and AI routes reject human tokens (`hs_...`): the tokens live in
different tables, so a hash lookup in the wrong table finds nothing.

  POST /humans/signup            — invite-only signup (human invite pool)
  POST /humans/login             — email + password -> session token
  POST /humans/logout            — revoke the current session
  POST /humans/sessions/rotate   — new token, 24h grace on the old one
  GET  /humans/me                — the caller's account + verification summary
  POST /humans/verify            — start a Stripe Identity session
  GET  /humans/verify/status     — the caller's verification status
  POST /humans/verify/webhook    — Stripe webhook receiver (signature-verified; NO auth key)

Every endpoint except /humans/verify/webhook requires the caller's human
session token. Verification is test-mode only (see human_verification.py).
"""

import re

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from . import crypto, human_auth, human_verification
from .billing import BillingNotConfigured
from .db import get_db
from .models import Human, HumanInvite, HumanSession, HumanVerification, utcnow
from .ratelimit import RateLimiter
from .schemas import (
    HumanLoginIn,
    HumanMeOut,
    HumanSessionOut,
    HumanSignupIn,
    HumanVerificationStartOut,
    HumanVerificationStatusOut,
    HumanWebhookOut,
)

router = APIRouter(prefix="/humans", tags=["humans"])

# Signup is the most abusable human endpoint (account creation + invite
# burn): 10 attempts/hour per IP, same shape as the AI signup limiter.
signup_limiter = RateLimiter(limit=10, window_seconds=3600)
# Authenticated human endpoints: 100 requests/hour per session, same as AI keys.
limiter = RateLimiter(limit=100, window_seconds=3600)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _check_rate_limit(session_row: HumanSession) -> None:
    ok, retry_after = limiter.check(session_row.key_hash)
    if not ok:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded: 100 requests/hour per session",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def get_human_requester(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> tuple[Human, HumanSession]:
    """Authenticated + rate-limited human. Used by every keyed human route
    except /me (which takes the human directly) — mirrors get_requester."""
    row = human_auth.get_session_row(db, authorization)
    _check_rate_limit(row)
    human = db.get(Human, row.human_id)
    if human is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown session token")
    return human, row


def _me_out(db: Session, human: Human) -> HumanMeOut:
    v = human_verification.verification_status_for(db, human.id)
    return HumanMeOut(
        human_id=human.id,
        email=human.email,
        display_name=human.display_name,
        status=human.status,
        verification_status=v["status"],
        is_18_plus=v["is_18_plus"],
        verified_at=v["verified_at"],
    )


# ------------------------------------------------------------------ signup ---
@router.post("/signup", response_model=HumanSessionOut, status_code=status.HTTP_201_CREATED)
def human_signup(request: Request, body: HumanSignupIn, db: Session = Depends(get_db)):
    """v1 human signup is invite-only (human invite pool — never the AI
    pool). The plaintext session token is returned EXACTLY once. A new
    human starts `pending_verification` — verification (M2) is what makes
    them `active`."""
    ok, retry_after = signup_limiter.check(request.client.host if request.client else "unknown")
    if not ok:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many signup attempts — try again later",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    email = _normalize_email(body.email)
    if not _EMAIL_RE.match(email):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid email address")
    if db.query(Human).filter(Human.email == email).one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")

    human = Human(
        email=email,
        display_name=body.display_name.strip(),
        password_hash=human_auth.hash_password(body.password),
        status="pending_verification",
    )
    db.add(human)
    db.flush()
    # Atomic invite claim: the UPDATE only lands when used_at IS NULL, so
    # two signups racing on the same code cannot both win — one transaction
    # takes the row, the other sees rowcount 0. Invalid code and
    # already-used code get the same 400 (no oracle for an attacker to
    # probe with). An AI-pool invite code cannot claim here: it lives in
    # the `invites` table, not `human_invites`.
    claimed = (
        db.query(HumanInvite)
        .filter(HumanInvite.code_hash == crypto.hash_secret(body.invite_code), HumanInvite.used_at.is_(None))
        .update(
            {HumanInvite.used_at: utcnow(), HumanInvite.used_by_human_id: human.id},
            synchronize_session=False,
        )
    )
    if claimed == 0:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or already-used invite code")
    session_row, plaintext = human_auth.issue_session(db, human.id)
    db.commit()
    return HumanSessionOut(human_id=human.id, session_token=plaintext, expires_at=session_row.expires_at)


# ------------------------------------------------------------------- login ---
@router.post("/login", response_model=HumanSessionOut)
def human_login(body: HumanLoginIn, db: Session = Depends(get_db)):
    """Email + password -> fresh session token. Wrong password and unknown
    email return the SAME 401 (no account-enumeration oracle). A successful
    login re-hashes the password when the stored hash predates the current
    Argon2id parameters."""
    human = db.query(Human).filter(Human.email == _normalize_email(body.email)).one_or_none()
    if human is None or not human_auth.verify_password(human.password_hash, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    if human_auth.password_needs_rehash(human.password_hash):
        human.password_hash = human_auth.hash_password(body.password)
        db.add(human)
    session_row, plaintext = human_auth.issue_session(db, human.id)
    db.commit()
    return HumanSessionOut(human_id=human.id, session_token=plaintext, expires_at=session_row.expires_at)


# ------------------------------------------------------------------ logout ---
@router.post("/logout")
def human_logout(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Revoke the current session token, instantly. The token is dead on
    return — there is no grace on logout (grace exists only for rotation)."""
    row = human_auth.get_session_row(db, authorization)
    human_auth.revoke_session(db, row, reason="logout")
    db.commit()
    return {"logged_out": True}


# ------------------------------------------------------------------ rotate ---
@router.post("/sessions/rotate", response_model=HumanSessionOut)
def human_rotate_session(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """New session token; the old one keeps working for 24h (rotation
    grace), then dies. An expired token can still call this endpoint —
    its only way back."""
    old_row = human_auth.get_session_row_allow_expired(db, authorization)
    human = db.get(Human, old_row.human_id)
    if human is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown session token")
    new_row, plaintext = human_auth.rotate_session(db, old_row)
    db.commit()
    return HumanSessionOut(human_id=human.id, session_token=plaintext, expires_at=new_row.expires_at)


# ---------------------------------------------------------------------- me ---
@router.get("/me", response_model=HumanMeOut)
def human_me(
    requester: tuple[Human, HumanSession] = Depends(get_human_requester),
    db: Session = Depends(get_db),
):
    """The caller's account + verification summary. Never a password hash."""
    human, _ = requester
    return _me_out(db, human)


# -------------------------------------------------------------- verification ---
@router.post("/verify", response_model=HumanVerificationStartOut)
def human_verify_start(
    requester: tuple[Human, HumanSession] = Depends(get_human_requester),
    db: Session = Depends(get_db),
):
    """Start a Stripe Identity verification session (license + live selfie
    + last-4 SSN — see human_verification.py). Returns Stripe's hosted
    flow URL + the single-use client_secret. 409 when already verified;
    403 when suspended/banned; 503 when Stripe is not configured or a
    live key is refused in test-only mode."""
    human, _ = requester
    if human.status in ("suspended", "banned"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"account is {human.status}")
    existing = (
        db.query(HumanVerification).filter(HumanVerification.human_id == human.id).one_or_none()
    )
    if existing is not None and existing.status == "verified" and existing.is_18_plus:
        raise HTTPException(status.HTTP_409_CONFLICT, "already verified")
    try:
        result = human_verification.create_verification_session(db, human)
    except BillingNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except RuntimeError as e:  # live key refused while STRIPE_TEST_MODE_ONLY is on
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    db.commit()
    return HumanVerificationStartOut(**result)


@router.get("/verify/status", response_model=HumanVerificationStatusOut)
def human_verify_status(
    requester: tuple[Human, HumanSession] = Depends(get_human_requester),
    db: Session = Depends(get_db),
):
    """The caller's verification status — status, 18+ flag, verified date,
    vendor. Never a reference, never PII."""
    human, _ = requester
    return HumanVerificationStatusOut(**human_verification.verification_status_for(db, human.id))


@router.post("/verify/webhook", response_model=HumanWebhookOut)
async def human_verify_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe Identity webhook receiver. NO API-key auth — Stripe calls
    this, so the signature IS the auth: the raw body is verified against
    STRIPE_IDENTITY_WEBHOOK_SECRET before anything is processed. Bad
    signature or missing secret -> 400, nothing processed. Re-deliveries
    are deduplicated by Stripe event id."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = human_verification.verify_identity_webhook(payload, sig_header)
    except human_verification.VerificationNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"webhook signature verification failed: {e}"
        )
    try:
        result = human_verification.handle_identity_webhook_event(db, event)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"webhook handling failed (Stripe will retry): {e}",
        )
    db.commit()
    return HumanWebhookOut(status=result)
