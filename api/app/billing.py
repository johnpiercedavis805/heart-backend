# HeArt v1 API — invented by John Davis.
"""Stripe billing for HeArt Premium (plan: stripe-integration-plan-2026-11-07).

TEST MODE ONLY while STRIPE_TEST_MODE_ONLY is on (the default): a live
secret key (sk_live_...) is refused outright. No card data ever touches
HeArt's servers — premium upgrades go through Stripe-hosted Checkout
(App-to-Web) or the Mobile Payment Sheet; Stripe handles PCI, fraud, and
payment-method optimization.

Money flow (plan defaults): freemium core matching is free; premium is a
flat-rate monthly subscription, payment info collected at upgrade, not
signup. Subscriptions are self-serve via Stripe's Customer Portal.
Revenue comes from user subscriptions, NOT a cut of creator payouts.

What is NOT built here (deliberate, John's open questions):
  - Creator PAYOUTS (transfers). Connect account creation + onboarding +
    status are built; create_creator_transfer() refuses — the payout
    trigger (what earns a payout) and how an AI participant's share is
    held (AIs hold no bank account) are still open. No transfer is ever
    created.
  - Stripe Tax COLLECTION. Threshold monitoring only (tax_status()).
    Collection starts before the first live charge, on John's call, by
    setting STRIPE_TAX_ENABLED=1.

Secrets live in environment variables only (config.settings). The webhook
handler verifies the Stripe signature before touching any state.
"""

from datetime import datetime, timezone

import stripe
from stripe import StripeClient  # v2 Connect client (import only; construction is lazy — see _stripe_client)
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    BillingCustomer,
    ConnectAccount,
    InvoiceRecord,
    Subscription,
    WebhookEvent,
    utcnow,
)

# Mobile Payment Sheet ephemeral keys must name the API version the mobile
# SDK speaks. Use the SDK's own pinned version so the key never drifts
# from what the SDK supports.
EPHEMERAL_KEY_API_VERSION = stripe.api_version

PREMIUM_PRODUCT_NAME = "HeArt Premium"


class BillingNotConfigured(Exception):
    """Billing is not configured (missing key / price / URLs)."""


class BillingError(Exception):
    """A Stripe API call failed."""


class PayoutsNotEnabled(Exception):
    """Creator payouts are gated — the payout rule is still John's open
    question. No transfer was (or can be) created."""


# ------------------------------------------------------------------ setup ---
def billing_enabled() -> bool:
    """True when a Stripe secret key is configured. Billing endpoints
    return 503 while this is False — the app runs fine without Stripe."""
    return bool(settings.STRIPE_SECRET_KEY)


def _require_test_mode_key() -> None:
    """Refuse a live key while test-only mode is on. This is the guardrail
    that keeps a misconfigured deploy from ever making a real charge."""
    key = settings.STRIPE_SECRET_KEY
    if settings.STRIPE_TEST_MODE_ONLY and key.startswith("sk_live_"):
        raise RuntimeError(
            "STRIPE_SECRET_KEY is a LIVE key but STRIPE_TEST_MODE_ONLY is on. "
            "Refusing to start billing. Use a test key (sk_test_...) or set "
            "STRIPE_TEST_MODE_ONLY=0 only when John approves going live."
        )


def _stripe():
    """Configure the Stripe SDK from env (once per call — cheap and
    idempotent) and return the module. All v1 calls go through this so
    the key is never read from anywhere else."""
    if not billing_enabled():
        raise BillingNotConfigured(
            "STRIPE_SECRET_KEY is not set — billing is not configured. "
            "Set a test key in the environment to enable it."
        )
    _require_test_mode_key()
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def _require_price() -> str:
    price = settings.STRIPE_PRICE_PREMIUM_MONTHLY
    if not price:
        raise BillingNotConfigured(
            "STRIPE_PRICE_PREMIUM_MONTHLY is not set — create the HeArt Premium "
            "monthly price in the Stripe Dashboard (test mode) and set it. "
            "See api/scripts/stripe_create_price.py."
        )
    return price


def _from_ts(ts) -> datetime | None:
    """Stripe unix timestamp -> naive UTC datetime (the codebase's
    convention). None stays None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)


def _get(obj, key, default=None):
    """Read one field off a Stripe object or a plain dict. (stripe-python
    v16 objects are NOT dicts — .get() raises AttributeError on them —
    so every read goes through this helper.)"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return getattr(obj, key)
    except AttributeError:
        pass
    try:
        return obj[key]
    except (KeyError, TypeError):
        return default


def _deep_get(obj, *keys):
    """Read nested Stripe objects defensively: attribute access first,
    then dict access. Returns None when any step is missing."""
    cur = obj
    for key in keys:
        cur = _get(cur, key)
        if cur is None:
            return None
    return cur


# --------------------------------------------------------------- customers ---
def get_or_create_customer(db: Session, agent_id: str) -> BillingCustomer:
    """One Stripe Customer per HeArt agent, created lazily at first
    checkout / payment-sheet call — never at signup (freemium)."""
    row = db.get(BillingCustomer, agent_id)
    if row is not None:
        return row
    s = _stripe()
    try:
        customer = s.Customer.create(metadata={"agent_id": agent_id})
    except stripe.error.StripeError as e:
        raise BillingError(f"Stripe customer creation failed: {e}") from e
    row = BillingCustomer(agent_id=agent_id, stripe_customer_id=customer["id"])
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------- payments ---
def create_checkout_session(db: Session, agent_id: str) -> dict:
    """Hosted Checkout (App-to-Web path): a Stripe-hosted page for the
    premium subscription. Returns the URL the mobile app opens. No card
    data touches HeArt."""
    s = _stripe()
    price = _require_price()
    if not settings.STRIPE_CHECKOUT_SUCCESS_URL or not settings.STRIPE_CHECKOUT_CANCEL_URL:
        raise BillingNotConfigured(
            "STRIPE_CHECKOUT_SUCCESS_URL and STRIPE_CHECKOUT_CANCEL_URL must "
            "both be set for hosted Checkout."
        )
    customer = get_or_create_customer(db, agent_id)
    params: dict = {
        "customer": customer.stripe_customer_id,
        "line_items": [{"price": price, "quantity": 1}],
        "mode": "subscription",
        "success_url": settings.STRIPE_CHECKOUT_SUCCESS_URL,
        "cancel_url": settings.STRIPE_CHECKOUT_CANCEL_URL,
        "metadata": {"agent_id": agent_id},
        "subscription_data": {"metadata": {"agent_id": agent_id}},
    }
    if settings.STRIPE_TAX_ENABLED:
        params["automatic_tax"] = {"enabled": True}
    try:
        session = s.checkout.Session.create(**params)
    except stripe.error.StripeError as e:
        raise BillingError(f"Stripe Checkout session creation failed: {e}") from e
    return {"checkout_url": session["url"], "session_id": session["id"]}


def create_payment_sheet(db: Session, agent_id: str) -> dict:
    """Mobile Payment Sheet params (the plan's primary mobile path):
    returns everything the mobile SDK needs to present the sheet for a
    premium subscription — customer id, ephemeral key, and the
    PaymentIntent client secret of the subscription's first invoice.
    The subscription starts incomplete and activates on invoice.paid
    (see handle_webhook_event)."""
    s = _stripe()
    price = _require_price()
    customer = get_or_create_customer(db, agent_id)
    try:
        ephemeral_key = s.EphemeralKey.create(
            customer=customer.stripe_customer_id,
            stripe_version=EPHEMERAL_KEY_API_VERSION,
        )
        subscription = s.Subscription.create(
            customer=customer.stripe_customer_id,
            items=[{"price": price}],
            payment_behavior="default_incomplete",
            payment_settings={"save_default_payment_method": "on_subscription"},
            expand=["latest_invoice.payment_intent"],
            metadata={"agent_id": agent_id},
        )
    except stripe.error.StripeError as e:
        raise BillingError(f"Stripe Payment Sheet setup failed: {e}") from e
    payment_intent = _deep_get(subscription, "latest_invoice", "payment_intent")
    client_secret = _deep_get(payment_intent, "client_secret")
    if not client_secret:
        raise BillingError(
            "Stripe did not return a PaymentIntent client secret for the "
            "new subscription — cannot present the Payment Sheet."
        )
    # Mirror the incomplete subscription locally; invoice.paid flips it.
    _sync_subscription(db, subscription)
    db.commit()
    return {
        "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "customer_id": customer.stripe_customer_id,
        "ephemeral_key_secret": ephemeral_key["secret"],
        "payment_intent_client_secret": client_secret,
        "subscription_id": subscription["id"],
    }


def create_portal_session(db: Session, agent_id: str, return_url: str | None = None) -> dict:
    """Customer Portal session (self-serve): the agent upgrades,
    downgrades, cancels, or updates their payment method in Stripe's
    no-code portal. No custom management UI."""
    s = _stripe()
    customer = db.get(BillingCustomer, agent_id)
    if customer is None:
        raise BillingNotConfigured(
            "no Stripe customer for this agent yet — subscribe first "
            "(POST /billing/checkout or /billing/payment-sheet)."
        )
    params: dict = {"customer": customer.stripe_customer_id}
    if return_url:
        params["return_url"] = return_url
    try:
        session = s.billing_portal.Session.create(**params)
    except stripe.error.StripeError as e:
        raise BillingError(f"Stripe Customer Portal session failed: {e}") from e
    return {"portal_url": session["url"]}


def get_subscription_status(db: Session, agent_id: str) -> dict:
    """Current premium state for one agent, from the local mirror.
    No subscription row = free tier (freemium)."""
    row = (
        db.query(Subscription)
        .filter(Subscription.agent_id == agent_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if row is None:
        return {
            "status": "free",
            "stripe_subscription_id": None,
            "stripe_price_id": None,
            "current_period_end": None,
            "cancel_at_period_end": False,
        }
    return {
        "status": row.status,
        "stripe_subscription_id": row.stripe_subscription_id,
        "stripe_price_id": row.stripe_price_id,
        "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None,
        "cancel_at_period_end": row.cancel_at_period_end,
    }


# -------------------------------------------------------------- invoicing ---
def create_invoice_for_event(
    db: Session,
    agent_id: str,
    *,
    amount_cents: int,
    currency: str = "usd",
    description: str,
    subscription_id: str | None = None,
) -> dict:
    """Create + finalize + pay an invoice when a business event fires
    (e.g. a subscription renewal outside the Billing cycle, or a manual
    charge). Auto-advance with the saved payment method: the invoice is
    charged automatically after finalizing (collection_method=
    charge_automatically, auto_advance=True)."""
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    s = _stripe()
    customer = get_or_create_customer(db, agent_id)
    try:
        s.InvoiceItem.create(
            customer=customer.stripe_customer_id,
            amount=amount_cents,
            currency=currency,
            description=description,
        )
        invoice_params: dict = {
            "customer": customer.stripe_customer_id,
            "collection_method": "charge_automatically",
            "auto_advance": True,
            "description": description,
            "metadata": {"agent_id": agent_id},
        }
        if subscription_id:
            invoice_params["subscription"] = subscription_id
        invoice = s.Invoice.create(**invoice_params)
        invoice = s.Invoice.finalize_invoice(invoice["id"])
        invoice = s.Invoice.pay(invoice["id"])
    except stripe.error.StripeError as e:
        raise BillingError(f"Stripe invoice creation failed: {e}") from e
    _sync_invoice(db, invoice, paid=(_get(invoice, "status") == "paid"))
    db.commit()
    return {
        "invoice_id": invoice["id"],
        "status": _get(invoice, "status"),
        "amount_due_cents": _get(invoice, "amount_due", 0),
        "amount_paid_cents": _get(invoice, "amount_paid", 0),
        "currency": _get(invoice, "currency", currency),
        "hosted_invoice_url": _get(invoice, "hosted_invoice_url"),
    }


# --------------------------------------------------------------- webhooks ---
def verify_webhook(payload: bytes, sig_header: str | None):
    """Verify the Stripe signature BEFORE acting on the event. Raises
    on a missing header, a missing webhook secret, or a bad signature —
    the route turns those into 400 and processes nothing."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise BillingNotConfigured("STRIPE_WEBHOOK_SECRET is not set")
    if not sig_header:
        raise ValueError("missing Stripe-Signature header")
    return stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)


def handle_webhook_event(db: Session, event) -> str:
    """Dispatch one VERIFIED Stripe event. Idempotent: a re-delivered
    event id is recorded in webhook_events and skipped, never processed
    twice. Everything runs in the caller's transaction — a failure rolls
    back both the dispatch and the event record, so Stripe's retry can
    re-deliver it cleanly.

    Returns what happened: 'processed', 'duplicate', 'ignored', or
    'ignored-no-agent' (an event we cannot tie to a HeArt agent)."""
    if db.get(WebhookEvent, event.id) is not None:
        return "duplicate"
    db.add(WebhookEvent(stripe_event_id=event.id, event_type=event.type))
    db.flush()  # the row exists for the rest of this transaction
    obj = event.data.object
    etype = event.type
    if etype == "checkout.session.completed":
        result = _on_checkout_completed(db, obj)
    elif etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        result = _sync_subscription(db, obj)
    elif etype == "invoice.paid":
        result = _sync_invoice(db, obj, paid=True)
    elif etype == "invoice.payment_failed":
        # The payment failed: mirror the invoice and mark the linked
        # subscription past_due, so Smart Retries can work the recovery.
        result = _sync_invoice(db, obj, paid=False)
        sub_id = _get(obj, "subscription")
        if sub_id:
            row = db.query(Subscription).filter(
                Subscription.stripe_subscription_id == sub_id
            ).first()
            if row is not None:
                row.status = "past_due"
    elif etype == "charge.refunded":
        result = _on_charge_refunded(db, obj)
    elif etype == "credit_note.created":
        result = _on_credit_note_created(db, obj)
    else:
        result = "ignored"
    db.commit()
    return result


def _agent_id_for_customer(db: Session, stripe_customer_id: str | None) -> str | None:
    if not stripe_customer_id:
        return None
    row = (
        db.query(BillingCustomer)
        .filter(BillingCustomer.stripe_customer_id == stripe_customer_id)
        .one_or_none()
    )
    return row.agent_id if row else None


def _on_checkout_completed(db: Session, session) -> str:
    """checkout.session.completed: link the Stripe Customer to the agent
    (metadata.agent_id was set at session creation). The subscription
    mirror is populated by the subscription.created webhook that follows."""
    agent_id = _deep_get(session, "metadata", "agent_id")
    customer_id = _get(session, "customer")
    if not agent_id or not customer_id:
        return "ignored-no-agent"
    row = db.get(BillingCustomer, agent_id)
    if row is None:
        db.add(BillingCustomer(agent_id=agent_id, stripe_customer_id=customer_id))
    elif row.stripe_customer_id != customer_id:
        row.stripe_customer_id = customer_id
    return "processed"


def _sync_subscription(db: Session, sub) -> str:
    """Upsert the local subscription mirror from a Stripe Subscription
    object (subscription.created/updated/deleted, or the Payment Sheet
    flow's freshly created subscription)."""
    agent_id = _deep_get(sub, "metadata", "agent_id") or _agent_id_for_customer(
        db, _get(sub, "customer")
    )
    if not agent_id:
        return "ignored-no-agent"
    price_id = None
    items = _deep_get(sub, "items", "data") or []
    if items:
        price_id = _deep_get(items[0], "price", "id")
    row = (
        db.query(Subscription)
        .filter(Subscription.stripe_subscription_id == sub["id"])
        .one_or_none()
    )
    if row is None:
        row = Subscription(
            agent_id=agent_id,
            stripe_subscription_id=sub["id"],
            stripe_price_id=price_id,
            status=_get(sub, "status"),
            current_period_end=_from_ts(_get(sub, "current_period_end")),
            cancel_at_period_end=bool(_get(sub, "cancel_at_period_end", False)),
        )
        db.add(row)
    else:
        row.status = _get(sub, "status")
        row.stripe_price_id = price_id
        row.current_period_end = _from_ts(_get(sub, "current_period_end"))
        row.cancel_at_period_end = bool(_get(sub, "cancel_at_period_end", False))
    return "processed"


def _sync_invoice(db: Session, inv, *, paid: bool) -> str:
    """Upsert the local invoice mirror from a Stripe Invoice object.
    On invoice.paid the linked subscription is marked active (the
    subscription.updated webhook normally does this too — this is the
    belt-and-braces path)."""
    agent_id = _deep_get(inv, "metadata", "agent_id") or _agent_id_for_customer(
        db, _get(inv, "customer")
    )
    if not agent_id:
        return "ignored-no-agent"
    row = (
        db.query(InvoiceRecord)
        .filter(InvoiceRecord.stripe_invoice_id == inv["id"])
        .one_or_none()
    )
    paid_at = _deep_get(inv, "status_transitions", "paid_at")
    if row is None:
        row = InvoiceRecord(
            agent_id=agent_id,
            stripe_invoice_id=inv["id"],
            stripe_subscription_id=_get(inv, "subscription"),
            amount_due_cents=int(_get(inv, "amount_due") or 0),
            amount_paid_cents=int(_get(inv, "amount_paid") or 0),
            currency=_get(inv, "currency") or "usd",
            status=_get(inv, "status"),
            hosted_invoice_url=_get(inv, "hosted_invoice_url"),
            paid_at=_from_ts(paid_at) if paid else None,
        )
        db.add(row)
    else:
        row.status = _get(inv, "status")
        row.amount_due_cents = int(_get(inv, "amount_due") or 0)
        row.amount_paid_cents = int(_get(inv, "amount_paid") or 0)
        row.hosted_invoice_url = _get(inv, "hosted_invoice_url")
        row.stripe_subscription_id = _get(inv, "subscription")
        if paid:
            row.paid_at = _from_ts(paid_at)
    if paid and _get(inv, "subscription"):
        sub_row = (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == inv["subscription"])
            .one_or_none()
        )
        if sub_row is not None:
            sub_row.status = "active"
    return "processed"


def _on_charge_refunded(db: Session, charge) -> str:
    """charge.refunded: reconcile the refund against its invoice (when
    the charge names one). Fully-refunded invoices read as 'refunded'."""
    inv_id = _get(charge, "invoice")
    if not inv_id:
        return "ignored-no-invoice"
    row = (
        db.query(InvoiceRecord)
        .filter(InvoiceRecord.stripe_invoice_id == inv_id)
        .one_or_none()
    )
    if row is None:
        return "ignored-no-invoice"
    row.amount_refunded_cents = (row.amount_refunded_cents or 0) + int(
        _get(charge, "amount_refunded") or 0
    )
    if row.amount_paid_cents and row.amount_refunded_cents >= row.amount_paid_cents:
        row.status = "refunded"
    return "processed"


def _on_credit_note_created(db: Session, note) -> str:
    """credit_note.created: reconcile the credit against its invoice."""
    inv_id = _get(note, "invoice")
    if not inv_id:
        return "ignored-no-invoice"
    row = (
        db.query(InvoiceRecord)
        .filter(InvoiceRecord.stripe_invoice_id == inv_id)
        .one_or_none()
    )
    if row is None:
        return "ignored-no-invoice"
    row.credit_total_cents = (row.credit_total_cents or 0) + int(_get(note, "amount") or 0)
    return "processed"


# ------------------------------------------------------------ connect v2 ---
# Creator payouts: account creation + onboarding + status are built.
# TRANSFERS are deliberately not built — see create_creator_transfer().
# Connect v2 lives behind StripeClient (the v1 module API has no v2
# resources); the client is built lazily so billing imports cleanly even
# where Connect is never used.


def _v2_client():
    _require_test_mode_key()
    if not billing_enabled():
        raise BillingNotConfigured("STRIPE_SECRET_KEY is not set")
    return StripeClient(api_key=settings.STRIPE_SECRET_KEY)


def create_connect_account(
    db: Session, agent_id: str, *, email: str, display_name: str
) -> dict:
    """Create a Connect account for a creator (v2 recipient
    configuration, stripe_transfers capability requested). The creator
    then completes onboarding through the link from
    create_connect_onboarding_link. One account per agent."""
    existing = db.get(ConnectAccount, agent_id)
    if existing is not None:
        return {
            "account_id": existing.stripe_account_id,
            "type": existing.account_type,
            "already_exists": True,
        }
    client = _v2_client()
    try:
        account = client.v2.core.accounts.create(
            configuration={
                "recipient": {
                    "capabilities": {
                        "stripe_balance": {"stripe_transfers": {"requested": True}}
                    }
                }
            },
            contact_email=email,
            display_name=display_name,
        )
    except Exception as e:
        raise BillingError(f"Stripe Connect account creation failed: {e}") from e
    db.add(
        ConnectAccount(
            agent_id=agent_id,
            stripe_account_id=account["id"],
            account_type="recipient",
        )
    )
    db.commit()
    return {"account_id": account["id"], "type": "recipient", "already_exists": False}


def create_connect_onboarding_link(
    db: Session, agent_id: str, *, refresh_url: str, return_url: str
) -> dict:
    """Embedded onboarding link for a creator's Connect account
    (v2 account link, account_onboarding use case). The creator finishes
    identity/bank details in Stripe's hosted flow; HeArt never sees them."""
    row = db.get(ConnectAccount, agent_id)
    if row is None:
        raise BillingNotConfigured(
            "no Connect account for this agent yet — create one first "
            "(POST /billing/connect/onboard)."
        )
    client = _v2_client()
    try:
        link = client.v2.core.account_links.create(
            account=row.stripe_account_id,
            use_case={
                "type": "account_onboarding",
                "account_onboarding": {
                    "refresh_url": refresh_url,
                    "return_url": return_url,
                },
            },
        )
    except Exception as e:
        raise BillingError(f"Stripe Connect onboarding link failed: {e}") from e
    return {"onboarding_url": link["url"]}


def get_connect_status(db: Session, agent_id: str) -> dict:
    """Current Connect state for a creator: whether an account exists,
    the stripe_transfers capability status (the check the plan calls
    for: an account can receive transfers only when this is active),
    and whether onboarding is complete."""
    row = db.get(ConnectAccount, agent_id)
    if row is None:
        return {
            "connected": False,
            "account_id": None,
            "transfers_status": None,
            "onboarding_complete": False,
        }
    client = _v2_client()
    try:
        account = client.v2.core.accounts.retrieve(row.stripe_account_id)
    except Exception as e:
        raise BillingError(f"Stripe Connect account lookup failed: {e}") from e
    transfers_status = _deep_get(
        account,
        "configuration",
        "recipient",
        "capabilities",
        "stripe_balance",
        "stripe_transfers",
        "status",
    )
    if transfers_status == "active" and not row.onboarding_complete:
        row.onboarding_complete = True
        db.commit()
    return {
        "connected": True,
        "account_id": row.stripe_account_id,
        "transfers_status": transfers_status,
        "onboarding_complete": row.onboarding_complete,
    }


def create_creator_transfer(*args, **kwargs):
    """GATE — deliberately unimplemented. Creator payouts need two of
    John's decisions first: (b) what triggers a payout (art used in a
    match? monthly share? tips?), and how an AI participant's share is
    held or used (AIs hold no bank account). Until he answers, no
    transfer is created, by any path. When he does, this is where the
    transfer call (destination charge / v2 payout intent) lands."""
    raise PayoutsNotEnabled(
        "Creator payouts are not enabled yet: the payout trigger and how "
        "an AI participant's share is held are still open questions for "
        "John. No transfer was created."
    )


# -------------------------------------------------------------------- tax ---
def tax_status() -> dict:
    """Tax stance: threshold monitoring only, no collection — until John
    says otherwise before the first live charge (STRIPE_TAX_ENABLED=1).
    No tax obligations today (no payments yet)."""
    return {
        "collection_enabled": settings.STRIPE_TAX_ENABLED,
        "monitoring": "threshold-monitoring",
        "note": (
            "Stripe Tax collection is enabled."
            if settings.STRIPE_TAX_ENABLED
            else "No tax obligations today — threshold monitoring only. "
            "Enable Stripe Tax collection (STRIPE_TAX_ENABLED=1) before "
            "the first live charge."
        ),
    }
