# HeArt v1 API — invented by John Davis.
"""Stripe billing endpoints (plan: stripe-integration-plan-2026-11-07).

  POST /billing/checkout        — hosted Checkout session (premium upgrade, A2W)
  POST /billing/payment-sheet   — Mobile Payment Sheet params (primary mobile path)
  POST /billing/portal          — Customer Portal session (self-serve management)
  GET  /billing/subscription    — current premium state for the caller
  POST /billing/invoices        — create + finalize + pay an invoice on a business event
  POST /billing/webhooks        — Stripe webhook receiver (signature-verified; NO auth key)
  GET  /billing/tax/status      — tax stance (monitoring only until John says otherwise)
  POST /billing/connect/onboard — create a Connect (v2 recipient) account for a creator
  POST /billing/connect/link    — embedded onboarding link for that account
  GET  /billing/connect/status  — Connect account + stripe_transfers capability state
  POST /billing/connect/payouts — process 50/50 payouts for one paid invoice
                                  (idempotent; retries failed rows, releases held
                                  rows whose creator has since onboarded)
  GET  /billing/connect/payouts — the caller's payout records as a creator
                                  (the queryable ledger, incl. AI held shares)

Every endpoint except /billing/webhooks requires the caller's API key
(the same auth as the rest of v1). Billing is disabled (503) while no
Stripe secret key is configured — the app runs fine without it.
"""

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from . import billing, funnel
from .config import settings
from .db import get_db
from .models import InvoiceRecord, Payout
from .ratelimit import RateLimiter
from .routes import get_requester
from .schemas import (
    CheckoutIn,
    CheckoutOut,
    ConnectLinkIn,
    ConnectLinkOut,
    ConnectOnboardIn,
    ConnectOnboardOut,
    ConnectStatusOut,
    InvoiceCreateIn,
    InvoiceOut,
    PaymentSheetIn,
    PaymentSheetOut,
    PayoutOut,
    PayoutProcessIn,
    PayoutProcessOut,
    PortalIn,
    PortalOut,
    SubscriptionOut,
    TaxStatusOut,
    WebhookOut,
)

router = APIRouter(prefix="/billing", tags=["billing"])
limiter = RateLimiter(settings.RATE_LIMIT_PER_HOUR, 3600)


def _check_rate_limit(key) -> None:
    ok, retry_after = limiter.check(key.key_hash)
    if not ok:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit exceeded: {settings.RATE_LIMIT_PER_HOUR} requests/hour per key",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def _billing_error(e: Exception) -> HTTPException:
    if isinstance(e, billing.AgentFreeError):
        return HTTPException(status.HTTP_409_CONFLICT, str(e))
    if isinstance(e, billing.BillingNotConfigured):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    if isinstance(e, billing.BillingError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    if isinstance(e, ValueError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    raise e


# --------------------------------------------------------------- payments ---
@router.post("/checkout", response_model=CheckoutOut)
def create_checkout(
    body: CheckoutIn = CheckoutIn(),
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Hosted Checkout for the premium subscription (App-to-Web). The
    mobile app opens checkout_url; Stripe handles the payment. Returns
    503 while billing is not configured. body.currency="usdc" uses the
    USDC-denominated price (same $9.99) — the crypto option appears in
    Checkout once stablecoins are enabled in the Stripe Dashboard."""
    agent, key = requester
    _check_rate_limit(key)
    try:
        out = CheckoutOut(**billing.create_checkout_session(db, agent.id, currency=body.currency))
    except Exception as e:
        funnel.record("checkout.failed", provider="stripe")
        raise _billing_error(e)
    funnel.record("checkout.created", provider="stripe")
    return out


@router.post("/payment-sheet", response_model=PaymentSheetOut)
def create_payment_sheet(
    body: PaymentSheetIn = PaymentSheetIn(),
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Mobile Payment Sheet params: customer id, ephemeral key, and the
    PaymentIntent client secret for the subscription's first invoice.
    The mobile SDK presents the sheet; the subscription activates on
    invoice.paid. body.currency="usdc" uses the USDC price — the mobile
    client confirms the PaymentIntent with payment method type crypto
    (mandate flow per Stripe's stablecoin-subscription guide)."""
    agent, key = requester
    _check_rate_limit(key)
    try:
        out = PaymentSheetOut(**billing.create_payment_sheet(db, agent.id, currency=body.currency))
    except Exception as e:
        funnel.record("checkout.failed", provider="stripe")
        raise _billing_error(e)
    # The Payment Sheet is a checkout too (the mobile checkout path) —
    # it counts under the same checkout.created counter.
    funnel.record("checkout.created", provider="stripe")
    return out


@router.post("/portal", response_model=PortalOut)
def create_portal(
    body: PortalIn,
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Customer Portal: self-serve upgrade / downgrade / cancel / update
    payment method. No custom management UI."""
    agent, key = requester
    _check_rate_limit(key)
    try:
        return PortalOut(**billing.create_portal_session(db, agent.id, body.return_url))
    except Exception as e:
        raise _billing_error(e)


@router.get("/subscription", response_model=SubscriptionOut)
def subscription_status(
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Current premium state for the caller. A registered agent with no
    paid source reports active with source "ai-free" (John's Nov 11,
    2026 call: AI agents are free); a paid source still wins when one
    exists. A non-agent id with no paid source is the free tier."""
    agent, key = requester
    _check_rate_limit(key)
    return SubscriptionOut(**billing.get_subscription_status(db, agent.id))


# -------------------------------------------------------------- invoicing ---
@router.post("/invoices", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
def create_invoice(
    body: InvoiceCreateIn,
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Create + finalize + pay an invoice when a business event fires.
    Auto-advance with the saved payment method."""
    agent, key = requester
    _check_rate_limit(key)
    try:
        return InvoiceOut(
            **billing.create_invoice_for_event(
                db,
                agent.id,
                amount_cents=body.amount_cents,
                currency=body.currency,
                description=body.description,
                subscription_id=body.subscription_id,
            )
        )
    except Exception as e:
        raise _billing_error(e)


# --------------------------------------------------------------- webhooks ---
@router.post("/webhooks", response_model=WebhookOut)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe webhook receiver. NO API-key auth — Stripe calls this, so
    the signature IS the auth: the raw body is verified against
    STRIPE_WEBHOOK_SECRET before anything is processed. Bad signature
    or missing secret -> 400, nothing processed. Re-deliveries are
    deduplicated by Stripe event id."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = billing.verify_webhook(payload, sig_header)
    except billing.BillingNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"webhook signature verification failed: {e}"
        )
    funnel.record("webhook.received", provider="stripe")
    try:
        result = billing.handle_webhook_event(db, event)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"webhook handling failed (Stripe will retry): {e}",
        )
    if result == "processed":
        funnel.record("webhook.processed", provider="stripe")
        if event.type == "checkout.session.completed":
            funnel.record("checkout.completed", provider="stripe")
    elif result == "duplicate":
        funnel.record("webhook.duplicate", provider="stripe")
    elif result.startswith("ignored"):
        funnel.record("webhook.ignored", provider="stripe")
    return WebhookOut(received=True, result=result)


# -------------------------------------------------------------------- tax ---
@router.get("/tax/status", response_model=TaxStatusOut)
def tax_status(requester=Depends(get_requester)):
    """Tax stance: threshold monitoring only, no collection — until John
    enables Stripe Tax collection before the first live charge."""
    _agent, key = requester
    _check_rate_limit(key)
    return TaxStatusOut(**billing.tax_status())


# ------------------------------------------------------------ connect v2 ---
@router.post("/connect/onboard", response_model=ConnectOnboardOut, status_code=status.HTTP_201_CREATED)
def connect_onboard(
    body: ConnectOnboardIn,
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Create a Connect account for a creator (v2 recipient config,
    stripe_transfers capability). One account per agent; repeat calls
    return the existing account. Payouts are NOT enabled by this."""
    agent, key = requester
    _check_rate_limit(key)
    try:
        return ConnectOnboardOut(
            **billing.create_connect_account(
                db, agent.id, email=body.email, display_name=body.display_name
            )
        )
    except Exception as e:
        raise _billing_error(e)


@router.post("/connect/link", response_model=ConnectLinkOut)
def connect_link(
    body: ConnectLinkIn,
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Embedded onboarding link for the creator's Connect account. The
    creator finishes identity/bank details in Stripe's hosted flow."""
    agent, key = requester
    _check_rate_limit(key)
    try:
        return ConnectLinkOut(
            **billing.create_connect_onboarding_link(
                db, agent.id, refresh_url=body.refresh_url, return_url=body.return_url
            )
        )
    except Exception as e:
        raise _billing_error(e)


@router.get("/connect/status", response_model=ConnectStatusOut)
def connect_status(
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Connect state for the caller: account exists? stripe_transfers
    capability status? onboarding complete?"""
    agent, key = requester
    _check_rate_limit(key)
    try:
        return ConnectStatusOut(**billing.get_connect_status(db, agent.id))
    except Exception as e:
        raise _billing_error(e)


@router.post("/connect/payouts", response_model=PayoutProcessOut, status_code=status.HTTP_201_CREATED)
def connect_payouts(
    body: PayoutProcessIn,
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Process the 50/50 creator payouts for one PAID invoice (John's
    calls, Nov 8: a subscription payment triggers a payout; half to the
    creators the subscriber matched with, half retained by HeArt).
    Idempotent: an invoice that already has payout rows returns them —
    re-processing never duplicates a row; it DOES retry "failed" rows
    and release "held" rows whose creator has since finished Connect
    onboarding. Human creators are paid by Stripe Transfer; AI creators'
    shares stay HELD in the ledger (no transfer is ever attempted for
    an AI share). 404 when no local invoice record exists for the id."""
    _agent, key = requester
    _check_rate_limit(key)
    invoice = (
        db.query(InvoiceRecord)
        .filter(InvoiceRecord.stripe_invoice_id == body.stripe_invoice_id)
        .one_or_none()
    )
    if invoice is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no invoice record for {body.stripe_invoice_id} — payouts "
            "run off the invoice.paid webhook's local mirror.",
        )
    already_processed = (
        db.query(Payout)
        .filter(Payout.stripe_invoice_id == body.stripe_invoice_id)
        .count()
        > 0
    )
    try:
        # PayPal invoices (id 'paypal:<sale id>') hold their money in
        # PayPal, not Stripe — their creator shares stay HELD, never a
        # Stripe Transfer. Stripe and Coinbase invoices transfer.
        allow_transfers = not body.stripe_invoice_id.startswith(
            billing.PAYPAL_INVOICE_PREFIX
        )
        rows = billing.process_payouts_for_invoice(
            db, invoice, allow_transfers=allow_transfers
        )
    except Exception as e:
        raise _billing_error(e)
    return PayoutProcessOut(
        payouts=[PayoutOut(**billing.payout_to_dict(r)) for r in rows],
        already_processed=already_processed,
    )


@router.get("/connect/payouts", response_model=list[PayoutOut])
def connect_payouts_list(
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """The caller's payout records as a creator — the queryable ledger.
    Includes AI held shares (with registry serial, amount, triggering
    payment, status "held") for the creator's specialists to find, and
    transferred/failed rows for human creators. Newest first."""
    agent, key = requester
    _check_rate_limit(key)
    rows = billing.list_payouts_for_creator(db, agent.id)
    return [PayoutOut(**billing.payout_to_dict(r)) for r in rows]
