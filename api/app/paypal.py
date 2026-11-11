# HeArt v1 API — invented by John Davis.
"""PayPal payments for HeArt Premium, via PayPal's own Subscriptions REST
API (John's call, Nov 9, 2026: cards + PayPal is enough — Venmo dropped,
Cash App Pay already built via Stripe).

READ THIS FIRST — why PayPal does NOT go through Stripe here (checked
Nov 10, 2026, against Stripe's own docs; see
docs/payment-methods-paypal-cashapp-venmo.md):
  - Stripe-native PayPal is "available to businesses in supported
    European countries" only. HeArt's Stripe account is a US business
    (Santa Barbara, CA), so Stripe will never offer PayPal at checkout
    for it — the code still lists "paypal" in Stripe's
    allowed_payment_method_types, but Stripe silently filters it out.
  - The other Stripe route (a custom payment method wired to HeArt's
    own PayPal account) does NOT support subscription mode for classic
    custom payment methods; the newer merchant-instructed-orchestration
    preview that does is a preview integration with its own adapter.
  - So this module uses PayPal's own Subscriptions API directly:
    product -> plan ($9.99/month) -> subscription -> PayPal handles
    the recurring billing. It is the GA, purpose-built recurring
    product, it has a real sandbox, and its webhooks cover the full
    lifecycle (activated, payment completed, cancelled, suspended).

How it works:
  - A subscription is priced by the plan (PAYPAL_PLAN_ID, $9.99/month
    — created once with api/scripts/paypal_create_plan.py). The buyer
    approves in PayPal's hosted flow (approval_url); PayPal owns the
    recurring charges. There is no card on file at HeArt — HeArt never
    sees the buyer's PayPal credentials or payment details.
  - On BILLING.SUBSCRIPTION.ACTIVATED the subscription is ACTIVE and
    the agent is premium (see billing.get_subscription_status, source
    "paypal"). Only the webhook activates premium — never the redirect
    back from PayPal alone (the redirect can arrive before PayPal has
    actually activated anything).
  - On each PAYMENT.SALE.COMPLETED (initial + every monthly renewal)
    the SAME 50/50 creator payout runs as for a Stripe subscription
    payment, computed on the USD sale amount — via a synthetic
    InvoiceRecord namespaced 'paypal:<sale id>' (there is no Stripe
    invoice for a PayPal payment; the payout ledger keys on that
    reference — same pattern as the Coinbase 'coinbase:<charge id>'
    namespace). See billing.process_payouts_for_invoice.
  - Money lands in HeArt's PayPal Business account, NOT in Stripe.
    The split is a LEDGER MIRROR only: no PayPal Payouts are made
    (moving money out to creators is a follow-up, not this module —
    see docs/paypal-subscriptions.md). Human creators' shares are
    recorded in the ledger exactly as the Stripe path records them;
    the Stripe Transfer step does not run for a PayPal payment because
    the money is not in Stripe. A human creator's PayPal share is
    therefore recorded as "held" with a note saying the funds sit in
    PayPal pending Payouts — never as "transferred".
  - Fee (PayPal US list): 3.49% + 49c per PayPal payment — about $0.84
    on $9.99, vs ~$0.59 on a Stripe card payment. Confirm on PayPal's
    fee schedule before quoting; the ledger records the gross amount
    (fees are PayPal's, not HeArt's split).

SANDBOX ONLY: PAYPAL_API_BASE defaults to the sandbox host
(api-m.sandbox.paypal.com) and the module refuses a live base
(api-m.paypal.com) while PAYPAL_TEST_MODE_ONLY is on. No live
credentials, no real money, no live charges — ever, in this module.
Keys live in env vars only (config.settings). The webhook handler
verifies the signature with PayPal's verify-webhook-signature
endpoint BEFORE touching any state.
"""

import base64
import json
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from . import billing, funnel
from .config import settings
from .models import InvoiceRecord, PayPalSubscription, utcnow

# One month of HeArt Premium, in USD cents — same price as the Stripe
# subscription (John's call, Nov 8: $9.99). The plan is priced in USD
# and PayPal charges the buyer $9.99/month on it.
PREMIUM_MONTHLY_USD_CENTS = 999
PREMIUM_MONTHLY_USD = "9.99"

# The payout ledger keys on an invoice id. A PayPal payment has no
# Stripe invoice, so its payout rows are namespaced under this prefix,
# keyed on the PayPal sale id (unique per monthly payment).
PAYPAL_INVOICE_PREFIX = "paypal:"

# PayPal's live API host. Anything else is treated as sandbox (the
# default base is api-m.sandbox.paypal.com).
PAYPAL_LIVE_HOST = "api-m.paypal.com"

# OAuth access tokens last ~9 hours; cache one in-process with a
# 60-second safety buffer so a normal request flow does one token call.
_TOKEN_CACHE: dict = {"access_token": None, "expires_at": None}


class PayPalNotConfigured(Exception):
    """PayPal is not configured (missing client id/secret, plan id, or
    webhook id) — or the module was asked to talk to live PayPal while
    test-only mode is on."""


class PayPalError(Exception):
    """A PayPal API call failed."""


def paypal_enabled() -> bool:
    """True when PayPal client credentials are configured. The PayPal
    endpoints return 503 while this is False — the app runs fine
    without it."""
    return bool(settings.PAYPAL_CLIENT_ID and settings.PAYPAL_CLIENT_SECRET)


def _require_sandbox() -> None:
    """Refuse the live PayPal host while test-only mode is on. This is
    the guardrail that keeps a misconfigured deploy from ever making a
    real charge."""
    if settings.PAYPAL_TEST_MODE_ONLY and PAYPAL_LIVE_HOST in settings.PAYPAL_API_BASE:
        raise PayPalNotConfigured(
            "PAYPAL_API_BASE is the LIVE PayPal host but "
            "PAYPAL_TEST_MODE_ONLY is on. Refusing to talk to live "
            "PayPal. Use the sandbox host (api-m.sandbox.paypal.com) or "
            "set PAYPAL_TEST_MODE_ONLY=0 only when John approves going live."
        )


def _require_credentials() -> None:
    if not paypal_enabled():
        raise PayPalNotConfigured(
            "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set — create "
            "a PayPal Developer REST app (sandbox) and set them. See "
            "docs/paypal-subscriptions.md."
        )


def _require_plan() -> str:
    if not settings.PAYPAL_PLAN_ID:
        raise PayPalNotConfigured(
            "PAYPAL_PLAN_ID is not set — create the $9.99/month plan "
            "with api/scripts/paypal_create_plan.py (sandbox) and set it. "
            "See docs/paypal-subscriptions.md."
        )
    return settings.PAYPAL_PLAN_ID


def _get_access_token() -> str:
    """OAuth2 client-credentials token, cached in-process until shortly
    before expiry. One token call per ~9 hours in normal operation."""
    now = datetime.now(timezone.utc)
    cached = _TOKEN_CACHE.get("access_token")
    expires_at = _TOKEN_CACHE.get("expires_at")
    if cached and expires_at and now < expires_at:
        return cached
    _require_credentials()
    _require_sandbox()
    url = settings.PAYPAL_API_BASE.rstrip("/") + "/v1/oauth2/token"
    basic = base64.b64encode(
        f"{settings.PAYPAL_CLIENT_ID}:{settings.PAYPAL_CLIENT_SECRET}".encode()
    ).decode()
    req = urllib.request.Request(
        url,
        data=b"grant_type=client_credentials",
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode(errors="replace") if e.fp else ""
        raise PayPalError(
            f"PayPal OAuth token request failed: HTTP {e.code} {detail}"
        ) from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise PayPalError(f"PayPal OAuth token request failed: {e}") from e
    token = data.get("access_token")
    if not token:
        raise PayPalError(f"PayPal did not return an access token: {data}")
    expires_in = int(data.get("expires_in") or 0)
    _TOKEN_CACHE["access_token"] = token
    _TOKEN_CACHE["expires_at"] = now + timedelta(seconds=max(expires_in - 60, 60))
    return token


def _paypal_request(method: str, path: str, payload: dict | None = None, *, _retried: bool = False) -> dict:
    """One raw HTTPS call to the PayPal REST API, with a Bearer token.
    Kept as its own function so tests can mock the HTTP boundary (no
    sandbox credentials exist in this environment) without touching
    urllib. A 401 clears the cached token and retries once — tokens
    can be revoked between calls."""
    _require_credentials()
    _require_sandbox()
    url = settings.PAYPAL_API_BASE.rstrip("/") + path
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {_get_access_token()}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": f"heart-{utcnow().strftime('%Y%m%d%H%M%S%f')}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401 and not _retried:
            _TOKEN_CACHE["access_token"] = None
            _TOKEN_CACHE["expires_at"] = None
            return _paypal_request(method, path, payload, _retried=True)
        detail = e.read()[:500].decode(errors="replace") if e.fp else ""
        raise PayPalError(
            f"PayPal API {method} {path} failed: HTTP {e.code} {detail}"
        ) from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise PayPalError(f"PayPal API {method} {path} failed: {e}") from e


def _approval_url(subscription: dict) -> str | None:
    """The buyer-approval link from a create-subscription response
    (the link with rel "approve"). None when PayPal did not return one."""
    for link in subscription.get("links") or []:
        if link.get("rel") == "approve":
            return link.get("href")
    return None


def _clean_paypal_url(url: str | None) -> str | None:
    """A return/cancel URL safe to send to PayPal: a literal URL with
    no {PLACEHOLDER} in it.

    PayPal's application_context return_url/cancel_url must be plain
    URLs. The Stripe checkout URLs carry a {CHECKOUT_SESSION_ID}
    placeholder that Stripe fills in on redirect — PayPal rejects
    those braces with INVALID_PARAMETER_SYNTAX (seen Nov 10, 2026:
    POST /paypal/subscribe with no body 502'd because the default
    return_url was the Stripe success URL). So: drop any query param
    whose value contains a {placeholder} (e.g. session_id=...), and
    refuse a URL whose path/fragment still contains braces (return
    None — the caller then omits the URL and PayPal uses its own
    default — rather than sending PayPal a URL it will reject).
    A URL with no placeholders comes back unchanged."""
    if not url:
        return None
    parsed = urlparse(url)
    params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if "{" not in v and "}" not in v
    ]
    cleaned = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params,
         urlencode(params), parsed.fragment)
    )
    if "{" in cleaned or "}" in cleaned:
        return None
    return cleaned or None


def _parse_paypal_time(value: str | None) -> datetime | None:
    """PayPal ISO-8601 timestamps ('2026-12-09T10:00:00Z') -> naive UTC
    datetime (the codebase's convention). None stays None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _amount_cents(amount: dict | None) -> int:
    """A PayPal amount object {"total": "9.99", "currency": "USD"} ->
    integer cents. Decimal math, never float — 9.99 * 100 in float is
    998.999... and would under-count by a cent."""
    if not amount:
        return 0
    try:
        return int(Decimal(str(amount.get("total") or "0")) * 100)
    except (InvalidOperation, ValueError, TypeError):
        return 0


# ---------------------------------------------------------- subscriptions ---
def create_paypal_subscription(
    db: Session,
    agent_id: str,
    *,
    return_url: str | None = None,
    cancel_url: str | None = None,
) -> dict:
    """Create a PayPal subscription for the $9.99/month plan and return
    the approval URL the buyer opens in PayPal. The subscription starts
    APPROVAL_PENDING; only the BILLING.SUBSCRIPTION.ACTIVATED webhook
    moves it to ACTIVE (never the redirect alone).

    Guards: an agent already premium via Stripe gets an error (no double
    billing); an agent already ACTIVE on PayPal gets their existing
    subscription back (no approval URL — it was already approved); a
    stale APPROVAL_PENDING row is replaced, so one agent never has two
    live approval links. A local PayPalSubscription row is created in
    status APPROVAL_PENDING.

    AI-FREE (John's call, Nov 11, 2026): a registered agent is refused
    up front (AgentFreeError) — agents never pay, on any rail."""
    billing.assert_payable(db, agent_id)
    _require_credentials()
    _require_sandbox()
    plan_id = _require_plan()
    # No double billing: an agent premium on another rail does not get a
    # second subscription.
    current = billing.get_subscription_status(db, agent_id)
    if current.get("status") == "active" and current.get("source") == "stripe":
        raise PayPalError(
            "agent is already premium via Stripe — cancel that "
            "subscription before starting a PayPal one."
        )
    if current.get("status") == "active" and current.get("source") == "paypal":
        row = (
            db.query(PayPalSubscription)
            .filter(
                PayPalSubscription.agent_id == agent_id,
                PayPalSubscription.status == "ACTIVE",
            )
            .order_by(PayPalSubscription.created_at.desc())
            .first()
        )
        return {
            "subscription_id": row.paypal_subscription_id if row else None,
            "approval_url": None,
            "status": "ACTIVE",
            "plan_id": plan_id,
            "already_active": True,
        }
    # Replace a stale unapproved subscription (the buyer never approved
    # it, so no money moved) rather than leaving two live approval links.
    stale = (
        db.query(PayPalSubscription)
        .filter(
            PayPalSubscription.agent_id == agent_id,
            PayPalSubscription.status == "APPROVAL_PENDING",
        )
        .all()
    )
    for row in stale:
        db.delete(row)
    payload: dict = {
        "plan_id": plan_id,
        "custom_id": agent_id,
        "application_context": {
            "brand_name": "HeArt",
            "user_action": "SUBSCRIBE_NOW",
            "shipping_preference": "NO_SHIPPING",
        },
    }
    # Return/cancel URLs, in precedence order (Nov 10, 2026 fix):
    #   1. explicit values from the request body — honored verbatim;
    #   2. PAYPAL_RETURN_URL / PAYPAL_CANCEL_URL env vars;
    #   3. the Stripe checkout URLs, CLEANED of their
    #      {CHECKOUT_SESSION_ID} placeholder (PayPal rejects the
    #      braces with INVALID_PARAMETER_SYNTAX — the old default sent
    #      the raw Stripe success URL and every no-body subscribe 502'd);
    #   4. nothing — the keys are omitted and PayPal uses its default.
    # The cleaned Stripe default lands on the shared /subscribe/success
    # and /subscribe/cancel pages (no session id for a PayPal buyer —
    # premium activates only on the ACTIVATED webhook, never the
    # redirect alone).
    return_url = (
        return_url
        or settings.PAYPAL_RETURN_URL
        or _clean_paypal_url(settings.STRIPE_CHECKOUT_SUCCESS_URL)
    )
    cancel_url = (
        cancel_url
        or settings.PAYPAL_CANCEL_URL
        or _clean_paypal_url(settings.STRIPE_CHECKOUT_CANCEL_URL)
    )
    if return_url:
        payload["application_context"]["return_url"] = return_url
    if cancel_url:
        payload["application_context"]["cancel_url"] = cancel_url
    resp = _paypal_request("POST", "/v1/billing/subscriptions", payload)
    subscription_id = resp.get("id")
    if not subscription_id:
        raise PayPalError(f"PayPal did not return a subscription id: {resp}")
    approval_url = _approval_url(resp)
    row = PayPalSubscription(
        agent_id=agent_id,
        paypal_subscription_id=subscription_id,
        paypal_plan_id=plan_id,
        status=resp.get("status") or "APPROVAL_PENDING",
    )
    db.add(row)
    db.commit()
    return {
        "subscription_id": subscription_id,
        "approval_url": approval_url,
        "status": row.status,
        "plan_id": plan_id,
        "already_active": False,
    }


def list_paypal_subscriptions(db: Session, agent_id: str) -> list[PayPalSubscription]:
    """The caller's PayPal subscriptions, newest first."""
    return (
        db.query(PayPalSubscription)
        .filter(PayPalSubscription.agent_id == agent_id)
        .order_by(PayPalSubscription.created_at.desc())
        .all()
    )


def paypal_subscription_to_dict(row: PayPalSubscription) -> dict:
    """One PayPal subscription as the API returns it."""
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "paypal_subscription_id": row.paypal_subscription_id,
        "paypal_plan_id": row.paypal_plan_id,
        "status": row.status,
        "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def active_paypal_subscription(db: Session, agent_id: str) -> PayPalSubscription | None:
    """The agent's ACTIVE PayPal subscription, or None. Used by
    billing.get_subscription_status (source "paypal")."""
    return (
        db.query(PayPalSubscription)
        .filter(
            PayPalSubscription.agent_id == agent_id,
            PayPalSubscription.status == "ACTIVE",
        )
        .order_by(PayPalSubscription.created_at.desc())
        .first()
    )


# --------------------------------------------------------------- webhooks ---
def verify_paypal_webhook(payload: bytes, headers: dict) -> bool:
    """Verify a PayPal webhook with PayPal's verify-webhook-signature
    endpoint BEFORE acting on the event. The five transmission headers
    + the webhook id (from the Developer Dashboard, NOT from the
    request) + the event exactly as received are posted back to PayPal,
    which answers verification_status SUCCESS or FAILURE. Only SUCCESS
    is trusted — anything else (including a failed verification call)
    returns False and the route rejects the event.

    Raises PayPalNotConfigured when the webhook id is not set, and
    ValueError when a transmission header is missing — the route turns
    those into 503 / 400 and processes nothing."""
    if not settings.PAYPAL_WEBHOOK_ID:
        raise PayPalNotConfigured(
            "PAYPAL_WEBHOOK_ID is not set — create the webhook in the "
            "PayPal Developer Dashboard (Apps & Credentials -> your app "
            "-> Webhooks) and set it. See docs/paypal-subscriptions.md."
        )
    required = {
        "auth_algo": headers.get("paypal-auth-algo"),
        "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError(f"missing PayPal webhook headers: {', '.join(missing)}")
    try:
        event = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"webhook payload is not valid JSON: {e}") from e
    verify_payload = {
        **required,
        "webhook_id": settings.PAYPAL_WEBHOOK_ID,
        "webhook_event": event,
    }
    try:
        resp = _paypal_request(
            "POST", "/v1/notifications/verify-webhook-signature", verify_payload
        )
    except PayPalError:
        return False
    return resp.get("verification_status") == "SUCCESS"


def handle_paypal_webhook(db: Session, event: dict) -> str:
    """Dispatch one VERIFIED PayPal webhook event. Returns what
    happened: 'processed', 'duplicate', 'ignored', or
    'ignored-no-subscription' (an event we cannot tie to a HeArt
    PayPal subscription).

    Handlers are idempotent: a re-delivered event changes nothing the
    second time (status sets are idempotent; a sale's payout is keyed
    on the synthetic invoice 'paypal:<sale id>', and
    process_payouts_for_invoice returns existing rows instead of
    duplicating them). The event id is recorded on the row as the last
    processed id, so an exact re-delivery is reported 'duplicate'.

    Only BILLING.SUBSCRIPTION.ACTIVATED activates premium, and only
    PAYMENT.SALE.COMPLETED triggers the 50/50 payout — one payout per
    monthly sale, on the USD sale amount."""
    event_id = event.get("id")
    etype = event.get("event_type")
    resource = event.get("resource") or {}
    subscription_id = resource.get("id")
    if etype == "PAYMENT.SALE.COMPLETED":
        # The sale's resource id is the SALE id; the subscription it
        # belongs to is billing_agreement_id.
        subscription_id = resource.get("billing_agreement_id")
    if not subscription_id:
        return "ignored-no-subscription"
    row = (
        db.query(PayPalSubscription)
        .filter(PayPalSubscription.paypal_subscription_id == subscription_id)
        .one_or_none()
    )
    if row is None:
        return "ignored-no-subscription"
    if event_id and row.paypal_event_id == event_id:
        return "duplicate"
    if etype == "BILLING.SUBSCRIPTION.ACTIVATED":
        prev_status = row.status
        row.status = "ACTIVE"
        next_billing = (resource.get("billing_info") or {}).get("next_billing_time")
        row.current_period_end = _parse_paypal_time(next_billing)
        funnel.record_subscription_flip(prev_status, "ACTIVE", provider="paypal")
        result = "processed"
    elif etype in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.EXPIRED"):
        prev_status = row.status
        row.status = etype.split(".")[-1]  # CANCELLED | SUSPENDED | EXPIRED
        funnel.record_subscription_flip(prev_status, row.status, provider="paypal")
        result = "processed"
    elif etype == "PAYMENT.SALE.COMPLETED":
        result = _on_sale_completed(db, row, resource)
    elif etype in (
        "BILLING.SUBSCRIPTION.CREATED",
        "BILLING.SUBSCRIPTION.UPDATED",
        "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
    ):
        # Lifecycle noise: no state change (a failed payment stays
        # ACTIVE in PayPal until its retry threshold suspends it — the
        # SUSPENDED webhook then flips it). Recorded, nothing else.
        result = "processed"
    else:
        return "ignored"
    row.paypal_event_id = event_id
    db.commit()
    return result


def _on_sale_completed(db: Session, row: PayPalSubscription, sale: dict) -> str:
    """A completed sale (initial + every monthly renewal): mirror it as
    a synthetic paid invoice and run the SAME 50/50 creator payout as
    a Stripe subscription payment, on the USD sale amount. The sale
    must be USD and completed — anything else is ignored (a pending or
    non-USD sale pays nobody)."""
    if (sale.get("state") or "").lower() != "completed":
        return "ignored"
    amount = sale.get("amount") or {}
    if (amount.get("currency") or "").upper() != "USD":
        return "ignored"
    amount_cents = _amount_cents(amount)
    if amount_cents <= 0:
        return "ignored"
    sale_id = sale.get("id")
    if not sale_id:
        return "ignored"
    invoice_id = PAYPAL_INVOICE_PREFIX + sale_id
    invoice = (
        db.query(InvoiceRecord)
        .filter(InvoiceRecord.stripe_invoice_id == invoice_id)
        .one_or_none()
    )
    if invoice is None:
        invoice = InvoiceRecord(
            agent_id=row.agent_id,
            stripe_invoice_id=invoice_id,
            stripe_subscription_id=row.paypal_subscription_id,
            amount_due_cents=amount_cents,
            amount_paid_cents=amount_cents,
            currency="usd",
            status="paid",
            paid_at=utcnow(),
        )
        db.add(invoice)
        # autoflush=False: flush before the payout lookup, or the new
        # row is invisible to process_payouts_for_invoice's query.
        db.flush()
    # allow_transfers=False: the money is in PayPal, not Stripe — the
    # creator half is recorded HELD in the ledger (PayPal Payouts are a
    # follow-up), never sent as a Stripe Transfer.
    billing.process_payouts_for_invoice(db, invoice, allow_transfers=False)
    return "processed"
