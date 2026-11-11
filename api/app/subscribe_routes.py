# HeArt v1 API — invented by John Davis.
"""Public subscribe pages (HeArt Premium storefront).

  GET /                   — landing page ("soul first, face later"),
                            links on to /subscribe
  GET /subscribe          — landing / subscribe page ($9.99/mo, posts the
                            buyer's agent API key to POST /billing/checkout
                            and redirects to Stripe's hosted Checkout)
  POST /invite-request    — invite capture: stores an email in
                            invite_requests (no email is sent yet — v1 is
                            invite-only while we test it)
  GET /subscribe/success  — Stripe redirects here after a completed payment
                            (session_id in the query string); the page can
                            check the live subscription status with the
                            buyer's key via GET /billing/subscription
  GET /subscribe/cancel   — Stripe redirects here when the buyer leaves
                            checkout unpaid; no charge was made, retry link

These pages take NO auth and NO API key — like /health. They are plain
static files served by FileResponse; the only dynamic behaviour is the
buyer's own browser calling the (already keyed) billing endpoints, plus
the invite capture above.
"""

import os
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from .db import engine, get_db
from .models import InviteRequest
from .ratelimit import RateLimiter
from .schemas import InviteRequestIn, InviteRequestOut

router = APIRouter(tags=["subscribe"])

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Public form, unauthenticated — a light per-IP guard so one address can't
# flood the invite list. Generous on purpose: a real person retries once.
_INVITE_LIMITER = RateLimiter(limit=10, window_seconds=3600)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_INVITE_OK = "You're on the list — we'll email your invite code when a spot opens."


def _page(filename: str) -> FileResponse:
    return FileResponse(
        os.path.join(_STATIC_DIR, filename), media_type="text/html; charset=utf-8"
    )


@router.get("/")
def landing_page() -> FileResponse:
    """HeArt landing page — soul first, face later."""
    return _page("index.html")


@router.get("/subscribe")
def subscribe_page() -> FileResponse:
    """HeArt Premium landing / subscribe page."""
    return _page("subscribe.html")


def _ensure_invite_requests_table() -> None:
    """Create invite_requests if it isn't there yet.

    The canonical home is migration 15 (supabase/migrations/
    20261114000000_heart_invite_requests.sql), applied with
    scripts/apply_migrations.py. This fallback exists so the live site
    never 500s on an invite capture before that migration lands — the
    table is created from the model (dialect-aware) and the insert is
    retried once. Safe to call when the table already exists
    (checkfirst=True).
    """
    InviteRequest.__table__.create(bind=engine, checkfirst=True)


@router.post("/invite-request", response_model=InviteRequestOut)
def invite_request(
    body: InviteRequestIn, request: Request, db: Session = Depends(get_db)
) -> InviteRequestOut:
    """Store an email for a HeArt v1 invite code. No email is sent yet."""
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        return JSONResponse(
            status_code=422,
            content={"detail": "Enter a valid email address."},
        )
    allowed, retry_after = _INVITE_LIMITER.check(request.client.host if request.client else "unknown")
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many invite requests — try again in {int(retry_after)} seconds."},
        )
    for attempt in range(2):
        try:
            existing = db.execute(
                select(InviteRequest).where(InviteRequest.email == email)
            ).scalar_one_or_none()
            if existing is not None:
                # Already on the list — same success reply, no second row,
                # no leak about whether the address was already there.
                return InviteRequestOut(message=_INVITE_OK)
            db.add(InviteRequest(email=email))
            db.commit()
            return InviteRequestOut(message=_INVITE_OK)
        except IntegrityError:
            # Lost a race with a duplicate insert — treat as already listed.
            db.rollback()
            return InviteRequestOut(message=_INVITE_OK)
        except (ProgrammingError, OperationalError):
            # Table missing (migration 15 not applied yet) or a transient
            # DB error: create the table from the model and retry once.
            db.rollback()
            if attempt == 0:
                _ensure_invite_requests_table()
                continue
            raise
    return InviteRequestOut(message=_INVITE_OK)


@router.get("/subscribe/success")
def subscribe_success_page() -> FileResponse:
    """Post-payment landing (Stripe success redirect target)."""
    return _page("success.html")


@router.get("/subscribe/cancel")
def subscribe_cancel_page() -> FileResponse:
    """Cancelled-checkout landing (Stripe cancel redirect target)."""
    return _page("cancel.html")
