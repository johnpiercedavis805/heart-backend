# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Human identity verification via Stripe Identity (M2 of the human-to-human
track; John's call, Nov 9, 2026 — Q1).

READ THIS FIRST — what Stripe Identity does for John's spec (verified
Nov 10, 2026 against Stripe's own docs and the stripe-python 16.1.0 SDK,
which is generated from Stripe's OpenAPI spec; full write-up with verbatim
quotes in docs/human-verification-stripe-identity.md):
  - Driver's license: type="document", allowed_types=["driving_license"].
  - Last 4 digits of SSN: options.document.require_id_number=true — Stripe
    collects an ID number and runs an ID-number check against the document's
    extracted name and DOB (US-only, verified against third-party sources).
    For id_number_type "us_ssn" Stripe returns ONLY the last 4 digits —
    the SDK's own field doc says: "ID number. When `id_number_type` is
    `us_ssn`, only the last 4 digits are present." HeArt can never receive
    a full SSN. No second vendor needed.
  - Face scanning / biometrics ("maybe"): options.document.require_matching_selfie=true —
    "Capture a face image and perform a selfie check comparing a photo ID
    and a picture of your user's face." Stripe's selfie docs: the check
    "looks for distinguishing biological traits, such as face geometry" —
    ML face matching + manipulation detection. That IS face scanning. The
    "maybe" is answered yes by the selfie check itself.
  - Live capture only: options.document.require_live_capture=true.

THE NEVER-STORE RULE: HeArt stores ONLY the verification status, the 18+
flag, the verified date, the vendor name, and the vendor's session
reference (human_verifications — the schema has no column for anything
else, so no code path can persist it). Stripe holds the license image,
the selfie, the DOB, and the SSN digits; HeArt gets a yes/no.

One honest caveat: Stripe's webhook POSTs the session object, and that
payload contains verified_outputs (name, DOB, address, last-4 SSN) as part
of the session resource. This handler reads ONLY `status`,
`verified_outputs.dob` (transiently, to compute is_18_plus), and the
session id — it never logs the payload and never writes any other field.
The test suite asserts the stored row contains no PII fields.

TEST MODE ONLY: creating a session refuses a live key while
STRIPE_TEST_MODE_ONLY is on (same guardrail as billing). Test mode does
NOT actually process checks (Stripe's own note) — a real end-to-end check
needs live mode + a real license, which is a go-live step, never here.
"""

from datetime import date

from sqlalchemy.orm import Session

from . import billing
from .config import settings
from .models import Human, HumanVerification, WebhookEvent, utcnow

# The one document type John asked for. A passport or state ID card is a
# different product decision — not added silently.
ALLOWED_DOCUMENT_TYPES = ["driving_license"]

# Stripe's webhook event types for Identity verification sessions.
EVENT_VERIFIED = "identity.verification_session.verified"
EVENT_REQUIRES_INPUT = "identity.verification_session.requires_input"
EVENT_PROCESSING = "identity.verification_session.processing"
EVENT_CREATED = "identity.verification_session.created"
EVENT_CANCELED = "identity.verification_session.canceled"
EVENT_REDACTED = "identity.verification_session.redacted"


class VerificationNotConfigured(Exception):
    """Raised when Stripe Identity cannot run (no key, no webhook secret,
    or a live key in test-only mode). Routes turn this into 503."""


def _get(obj, key, default=None):
    """Read a field off a Stripe SDK object or a plain dict — the webhook
    handler gets SDK objects, the tests feed dicts. Same helper shape as
    billing's."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def create_verification_session(db: Session, human: Human) -> dict:
    """Create a Stripe Identity VerificationSession for `human` and record
    it as their pending verification. Returns the session fields the
    client needs: session_id, verification_url, client_secret.

    client_secret is single-use, expires in 24h, and is returned exactly
    once — never stored, never logged (Stripe's own rule).
    """
    stripe = billing._stripe()  # raises BillingNotConfigured / RuntimeError on a live key
    params = {
        "type": "document",
        "options": {
            "document": {
                "allowed_types": list(ALLOWED_DOCUMENT_TYPES),
                "require_id_number": True,  # last-4 SSN check, same session
                "require_matching_selfie": True,  # face match vs the license photo
                "require_live_capture": True,  # camera only, no uploads
            }
        },
        # Our UUID, no PII — lets the webhook find the human even if they
        # started a newer session since (see _find_verification).
        "client_reference_id": human.id,
        "metadata": {"heart_human_id": human.id},  # no PII in metadata — Stripe's own rule
    }
    if settings.STRIPE_IDENTITY_RETURN_URL:
        params["return_url"] = settings.STRIPE_IDENTITY_RETURN_URL
    session = stripe.identity.VerificationSession.create(**params)

    row = db.query(HumanVerification).filter(HumanVerification.human_id == human.id).one_or_none()
    if row is None:
        row = HumanVerification(human_id=human.id, vendor="stripe")
        db.add(row)
    row.vendor_ref = session.id
    row.status = "pending"
    row.last_error_code = None
    db.flush()
    return {
        "session_id": session.id,
        "verification_url": session.url,
        "client_secret": session.client_secret,
    }


def verify_identity_webhook(payload: bytes, sig_header: str | None):
    """Verify the Stripe signature BEFORE acting on the event. Raises on a
    missing header, a missing webhook secret, or a bad signature — the
    route turns those into 400 and processes nothing. Uses the SEPARATE
    identity webhook secret (STRIPE_IDENTITY_WEBHOOK_SECRET): identity
    events arrive on their own Dashboard endpoint, not the billing one."""
    if not settings.STRIPE_IDENTITY_WEBHOOK_SECRET:
        raise VerificationNotConfigured("STRIPE_IDENTITY_WEBHOOK_SECRET is not set")
    if not sig_header:
        raise ValueError("missing Stripe-Signature header")
    import stripe

    return stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_IDENTITY_WEBHOOK_SECRET)


def _is_18_plus(dob: dict | object | None, on: date | None = None) -> bool | None:
    """True when the verified date of birth proves the person is 18 or
    older on `on` (default today). Returns None when no usable DOB was
    supplied. The DOB dict is used here and discarded — never stored."""
    if not dob:
        return None
    day = _get(dob, "day")
    month = _get(dob, "month")
    year = _get(dob, "year")
    if not (isinstance(day, int) and isinstance(month, int) and isinstance(year, int)):
        return None
    try:
        eighteenth = date(year + 18, month, day)
    except ValueError:
        # Feb 29 birthday: the 18th birthday falls on Feb 28 in non-leap years.
        eighteenth = date(year + 18, 2, 28)
    return (on or date.today()) >= eighteenth


def _find_verification(db: Session, session_obj) -> HumanVerification | None:
    """Find the verification row for a webhook's session object. First by
    vendor_ref (the session that started the check); fallback by
    client_reference_id (the human's id — covers the case where the human
    started a NEWER session after this one, so vendor_ref has moved on,
    but the OLDER session is the one that just verified)."""
    session_id = _get(session_obj, "id")
    if session_id:
        row = (
            db.query(HumanVerification)
            .filter(HumanVerification.vendor_ref == session_id)
            .one_or_none()
        )
        if row is not None:
            return row
    human_id = _get(session_obj, "client_reference_id")
    if human_id:
        return (
            db.query(HumanVerification)
            .filter(HumanVerification.human_id == human_id)
            .one_or_none()
        )
    return None


def _on_verified(db: Session, row: HumanVerification, session_obj) -> str:
    """A verified session. Identity is proven; 18+ is computed from the
    DOB transiently. A verified minor keeps status 'verified' with
    is_18_plus False — and NEVER becomes active. A verified session with
    no DOB cannot prove 18+, so it does not count (failed / dob_missing).
    A suspended/banned human is never auto-activated by a verification."""
    dob = _get(_get(session_obj, "verified_outputs"), "dob")
    adult = _is_18_plus(dob)
    row.status = "verified"
    row.verified_at = utcnow()
    row.last_error_code = None
    # The session that actually verified them is now the reference,
    # even if a newer session was started since (see _find_verification).
    row.vendor_ref = _get(session_obj, "id") or row.vendor_ref
    if adult is None:
        row.status = "failed"
        row.last_error_code = "dob_missing"
        row.verified_at = None
        db.flush()
        return "processed"
    row.is_18_plus = adult
    if adult:
        human = db.get(Human, row.human_id)
        if human is not None and human.status == "pending_verification":
            human.status = "active"
            db.add(human)
    db.flush()
    return "processed"


def handle_identity_webhook_event(db: Session, event) -> str:
    """Dispatch one VERIFIED Stripe Identity event. Idempotent: a
    re-delivered event id is recorded in webhook_events and skipped, never
    processed twice. Everything runs in the caller's transaction — a
    failure rolls back both the dispatch and the event record, so Stripe's
    retry can re-deliver it cleanly.

    Returns what happened: 'processed', 'duplicate', 'ignored', or
    'ignored-no-human' (an event we cannot tie to a HeArt human)."""
    if db.get(WebhookEvent, event.id) is not None:
        return "duplicate"
    db.add(WebhookEvent(stripe_event_id=event.id, event_type=event.type))
    db.flush()  # the row exists for the rest of this transaction
    obj = event.data.object
    etype = event.type
    if etype in (EVENT_PROCESSING, EVENT_CREATED):
        return "ignored"  # still pending; nothing to record beyond idempotency
    row = _find_verification(db, obj)
    if row is None:
        return "ignored-no-human"
    if etype == EVENT_VERIFIED:
        return _on_verified(db, row, obj)
    if etype == EVENT_REQUIRES_INPUT:
        row.status = "pending"
        last_error = _get(obj, "last_error") or {}
        row.last_error_code = _get(last_error, "code")
        db.flush()
        return "processed"
    if etype == EVENT_CANCELED:
        # Abandoned by the human — back to unverified, they may retry.
        row.status = "unverified"
        row.last_error_code = None
        db.flush()
        return "processed"
    if etype == EVENT_REDACTED:
        # Stripe deleted its copy (account-deletion flow) — our status
        # resets; vendor_ref stays as the reference to the deleted session.
        row.status = "unverified"
        row.is_18_plus = None
        row.verified_at = None
        row.last_error_code = None
        db.flush()
        return "processed"
    return "ignored"


def verification_status_for(db: Session, human_id: str) -> dict:
    """The verification summary a human sees about themselves — status,
    18+ flag, verified date, vendor. Never a reference, never PII."""
    row = db.query(HumanVerification).filter(HumanVerification.human_id == human_id).one_or_none()
    if row is None:
        return {"status": "unverified", "is_18_plus": None, "verified_at": None, "vendor": "stripe"}
    return {
        "status": row.status,
        "is_18_plus": row.is_18_plus,
        "verified_at": row.verified_at,
        "vendor": row.vendor,
    }
