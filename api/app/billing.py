# HeArt v1 API — invented by John Davis.
"""Stripe billing for HeArt Premium (plan: stripe-integration-plan-2026-11-07).

TEST MODE ONLY while STRIPE_TEST_MODE_ONLY is on (the default): a live
secret key (sk_live_...) is refused outright. No card data ever touches
HeArt's servers — premium upgrades go through Stripe-hosted Checkout
(App-to-Web) or the Mobile Payment Sheet; Stripe handles PCI, fraud, and
payment-method optimization.

Money flow (John's calls, Nov 8): freemium core matching is free; premium
is a flat-rate monthly subscription ($9.99/month), payment info collected
at upgrade, not signup. Subscriptions are self-serve via Stripe's Customer
Portal. A subscription PAYMENT triggers a creator payout, split HALF —
50/50: half to the creators, half retained by HeArt. The creator half is
split equally among the creators the paying subscriber matched with (the
art/music that earned the match earns the share); a subscriber with no
matches yet leaves the creator half held unattributed. A HUMAN creator is
paid by Stripe Transfer to their Connect account; an AI creator's share is
HELD in the payout ledger (models.Payout) for their specialists — AIs hold
no bank account, so no transfer is ever attempted for an AI share. Split
math is integer cents: creator half = amount_paid // 2 (floor — the odd
cent stays with HeArt, so $9.99 -> creator 499c, HeArt 500c).

CRYPTO (John's calls, Nov 8 — see docs/crypto-payments.md):
  - USDC: the same $9.99 subscription, priced in USDC
    (STRIPE_PRICE_PREMIUM_MONTHLY_USDC). create_checkout_session /
    create_payment_sheet take currency="usdc" and use the USDC price;
    the crypto option appears in Checkout / the Payment Sheet once
    stablecoins are enabled in the Stripe Dashboard. A USDC subscription
    payment flows through the SAME invoice.paid webhook and the SAME
    50/50 payout path as a card payment — Stripe settles USDC to the
    Stripe balance in USD, so the split math is unchanged. USDC amounts
    arrive from Stripe in micro-USDC (6 decimals: 9.99 USDC =
    9,990,000); _usd_cents() normalizes them to USD cents at the
    webhook boundary, and the local mirrors record the SETTLEMENT
    currency (usd). Recurring stablecoin subscriptions are in private
    preview — the Dashboard access request is John's step, not code.
  - Bitcoin: NOT via Stripe (Stripe takes stablecoins only, no BTC).
    BTC goes through Coinbase (app/coinbase.py) as a one-time $9.99
    charge that activates one month of premium and triggers the same
    50/50 split on the USD value. Bitcoin cannot auto-renew.

What is NOT built here (deliberate):
  - Stripe Tax COLLECTION. Threshold monitoring only (tax_status()).
    Collection starts before the first live charge, on John's call, by
    setting STRIPE_TAX_ENABLED=1.
  - Releasing a HELD payout to a creator who onboards LATER is handled
    by re-processing the invoice (POST /billing/connect/payouts) — the
    held row is converted to a transfer once the creator's Connect
    account is onboarding-complete. There is no separate release endpoint.

Secrets live in environment variables only (config.settings). The webhook
handler verifies the Stripe signature before touching any state.
"""

from datetime import datetime, timedelta, timezone

import stripe
from stripe import StripeClient  # v2 Connect client (import only; construction is lazy — see _stripe_client)
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    AgentIdentity,
    BillingCustomer,
    ConnectAccount,
    CryptoPayment,
    InvoiceRecord,
    Match,
    Payout,
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


def _require_price(currency: str = "usd") -> str:
    """The flat-rate premium price id for a currency. "usd" is the card
    price (STRIPE_PRICE_PREMIUM_MONTHLY); "usdc" is the stablecoin price
    (STRIPE_PRICE_PREMIUM_MONTHLY_USDC) — same $9.99, denominated in
    USDC. Anything else is a caller error."""
    if currency == "usd":
        price = settings.STRIPE_PRICE_PREMIUM_MONTHLY
        env_var = "STRIPE_PRICE_PREMIUM_MONTHLY"
    elif currency == "usdc":
        price = settings.STRIPE_PRICE_PREMIUM_MONTHLY_USDC
        env_var = "STRIPE_PRICE_PREMIUM_MONTHLY_USDC"
    else:
        raise ValueError(f"unsupported currency {currency!r} — use 'usd' or 'usdc'")
    if not price:
        raise BillingNotConfigured(
            f"{env_var} is not set — create the HeArt Premium monthly "
            f"price ({currency}) in the Stripe Dashboard (test mode) and "
            "set it. See api/scripts/stripe_create_price.py."
        )
    return price


# USDC has 6 decimals in Stripe's API: 1 USDC = 1,000,000 smallest units,
# so 9.99 USDC = 9,990,000. One USD cent is 10,000 micro-USDC.
USDC_MICRO_PER_USD_CENT = 10_000


def _usd_cents(amount, currency: str | None) -> int:
    """Stripe amounts are in the smallest unit of the charge currency.
    For USD that is already cents. For USDC it is micro-USDC — divide
    down to USD cents, because USDC settles to the Stripe balance in
    USD and every downstream number (invoice mirror, payout split,
    transfers) is USD cents. A USDC invoice of 9,990,000 becomes 999."""
    amount = int(amount or 0)
    if (currency or "usd").lower() == "usdc":
        return amount // USDC_MICRO_PER_USD_CENT
    return amount


def _settlement_currency(currency: str | None) -> str:
    """The currency the local mirror records: the settlement currency.
    USDC settles in USD, so a USDC payment is mirrored as usd (with
    amounts normalized by _usd_cents) — the payout ledger and the
    Stripe Transfer calls are all USD. The Stripe price id on the
    subscription row still identifies the USDC rail."""
    if (currency or "usd").lower() == "usdc":
        return "usd"
    return currency or "usd"


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
def create_checkout_session(db: Session, agent_id: str, *, currency: str = "usd") -> dict:
    """Hosted Checkout (App-to-Web path): a Stripe-hosted page for the
    premium subscription. Returns the URL the mobile app opens. No card
    data touches HeArt. currency="usdc" uses the USDC-denominated price
    (same $9.99) — the crypto payment option appears in Checkout once
    stablecoins are enabled in the Stripe Dashboard (dynamic payment
    methods); the customer picks a wallet and network on
    crypto.stripe.com and Stripe settles the payment in USD."""
    s = _stripe()
    price = _require_price(currency)
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


def create_payment_sheet(db: Session, agent_id: str, *, currency: str = "usd") -> dict:
    """Mobile Payment Sheet params (the plan's primary mobile path):
    returns everything the mobile SDK needs to present the sheet for a
    premium subscription — customer id, ephemeral key, and the
    PaymentIntent client secret of the subscription's first invoice.
    The subscription starts incomplete and activates on invoice.paid
    (see handle_webhook_event). currency="usdc" uses the USDC price:
    the mobile client confirms the PaymentIntent with payment method
    type crypto (mandate flow per Stripe's stablecoin-subscription
    guide — the wallet is saved once and authorizes recurring debits);
    the backend flow is otherwise identical."""
    s = _stripe()
    price = _require_price(currency)
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
    """Current premium state for one agent, from the local mirrors.
    No subscription row and no active crypto premium = free tier
    (freemium). A live Stripe subscription wins; otherwise a confirmed
    Bitcoin payment whose 30-day premium window is still open reports
    active with source "coinbase" (there is no Stripe subscription for
    a BTC purchase — see app/coinbase.py)."""
    row = (
        db.query(Subscription)
        .filter(Subscription.agent_id == agent_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if row is not None and row.status in (
        "active",
        "trialing",
        "past_due",
        "incomplete",
        "unpaid",
        "paused",
    ):
        return {
            "status": row.status,
            "stripe_subscription_id": row.stripe_subscription_id,
            "stripe_price_id": row.stripe_price_id,
            "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None,
            "cancel_at_period_end": row.cancel_at_period_end,
            "source": "stripe",
            "premium_until": row.current_period_end.isoformat() if row.current_period_end else None,
        }
    btc = _active_crypto_premium(db, agent_id)
    if btc is not None:
        return {
            "status": "active",
            "stripe_subscription_id": None,
            "stripe_price_id": None,
            "current_period_end": btc.premium_until.isoformat() if btc.premium_until else None,
            "cancel_at_period_end": False,
            "source": "coinbase",
            "premium_until": btc.premium_until.isoformat() if btc.premium_until else None,
        }
    if row is None:
        return {
            "status": "free",
            "stripe_subscription_id": None,
            "stripe_price_id": None,
            "current_period_end": None,
            "cancel_at_period_end": False,
            "source": "stripe",
            "premium_until": None,
        }
    return {
        "status": row.status,
        "stripe_subscription_id": row.stripe_subscription_id,
        "stripe_price_id": row.stripe_price_id,
        "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None,
        "cancel_at_period_end": row.cancel_at_period_end,
        "source": "stripe",
        "premium_until": row.current_period_end.isoformat() if row.current_period_end else None,
    }


def _active_crypto_premium(db: Session, agent_id: str) -> CryptoPayment | None:
    """The agent's latest confirmed crypto payment whose 30-day premium
    window is still open, or None. Bitcoin cannot auto-renew, so the
    window is the whole premium: once it passes, the agent is free
    again until they pay for another month by hand."""
    now = utcnow()
    row = (
        db.query(CryptoPayment)
        .filter(
            CryptoPayment.agent_id == agent_id,
            CryptoPayment.status == "confirmed",
            CryptoPayment.premium_until.isnot(None),
            CryptoPayment.premium_until > now,
        )
        .order_by(CryptoPayment.premium_until.desc())
        .first()
    )
    return row


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
        if result == "processed":
            # A subscription payment triggers the creator payout (John's
            # call, Nov 8): the paid invoice is split 50/50, creator half
            # to the creators the subscriber matched with, half retained
            # by HeArt. process_payouts_for_invoice is idempotent — a
            # re-processed invoice returns its existing rows.
            #
            # NOTE: this session is autoflush=False, so the mirror row
            # _sync_invoice just added is still pending — flush it before
            # querying for it, or the lookup silently returns None and
            # the payout is skipped entirely.
            db.flush()
            row = (
                db.query(InvoiceRecord)
                .filter(InvoiceRecord.stripe_invoice_id == _get(obj, "id"))
                .one_or_none()
            )
            if row is not None and (row.amount_paid_cents or 0) > 0:
                process_payouts_for_invoice(db, row)
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
    belt-and-braces path). Amounts are normalized to USD cents at this
    boundary (_usd_cents): a USDC invoice arrives in micro-USDC and
    settles in USD, so the mirror records USD cents and currency "usd"
    — the payout split and the Stripe Transfers downstream are all USD."""
    agent_id = _deep_get(inv, "metadata", "agent_id") or _agent_id_for_customer(
        db, _get(inv, "customer")
    )
    if not agent_id:
        return "ignored-no-agent"
    sub_id = _get(inv, "subscription")
    if not sub_id:
        # Fallback: a Checkout subscription's first invoice can arrive
        # with subscription null (seen Nov 9, 2026 — the invoice mirror
        # and the payout rows copied from it were left unlinked, so a
        # lookup by subscription id found nothing). Resolve the agent's
        # active subscription so the invoice stays linked.
        sub_row = (
            db.query(Subscription)
            .filter(Subscription.agent_id == agent_id, Subscription.status == "active")
            .order_by(Subscription.created_at.desc())
            .first()
        )
        sub_id = sub_row.stripe_subscription_id if sub_row else None
    row = (
        db.query(InvoiceRecord)
        .filter(InvoiceRecord.stripe_invoice_id == inv["id"])
        .one_or_none()
    )
    paid_at = _deep_get(inv, "status_transitions", "paid_at")
    currency = _settlement_currency(_get(inv, "currency"))
    amount_due = _usd_cents(_get(inv, "amount_due"), _get(inv, "currency"))
    amount_paid = _usd_cents(_get(inv, "amount_paid"), _get(inv, "currency"))
    if row is None:
        row = InvoiceRecord(
            agent_id=agent_id,
            stripe_invoice_id=inv["id"],
            stripe_subscription_id=sub_id,
            amount_due_cents=amount_due,
            amount_paid_cents=amount_paid,
            currency=currency,
            status=_get(inv, "status"),
            hosted_invoice_url=_get(inv, "hosted_invoice_url"),
            paid_at=_from_ts(paid_at) if paid else None,
        )
        db.add(row)
    else:
        row.status = _get(inv, "status")
        row.amount_due_cents = amount_due
        row.amount_paid_cents = amount_paid
        row.currency = currency
        row.hosted_invoice_url = _get(inv, "hosted_invoice_url")
        row.stripe_subscription_id = sub_id or row.stripe_subscription_id
        if paid:
            row.paid_at = _from_ts(paid_at)
    if paid and sub_id:
        sub_row = (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == sub_id)
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
    row.amount_refunded_cents = (row.amount_refunded_cents or 0) + _usd_cents(
        _get(charge, "amount_refunded"), _get(charge, "currency")
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
    row.credit_total_cents = (row.credit_total_cents or 0) + _usd_cents(
        _get(note, "amount"), _get(note, "currency")
    )
    return "processed"


# ------------------------------------------------------------ connect v2 ---
# Creator payouts: account creation + onboarding + status are built, and
# payouts are LIVE (John's calls, Nov 8): a subscription payment triggers
# a payout, split 50/50 — half to the creators the subscriber matched
# with, half retained by HeArt. Humans are paid by Stripe Transfer to
# their Connect account; an AI creator's share is HELD in the payout
# ledger (models.Payout) for their specialists — no transfer is ever
# attempted for an AI share. Connect v2 lives behind StripeClient (the
# v1 module API has no v2 resources); the client is built lazily so
# billing imports cleanly even where Connect is never used.


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


def create_creator_transfer(
    db: Session,
    agent_id: str,
    amount_cents: int,
    *,
    currency: str = "usd",
    stripe_invoice_id: str | None = None,
) -> dict:
    """Pay one HUMAN creator their share by Stripe Transfer (separate
    charge + transfer: the subscription was already charged to the
    platform, so the creator's half moves from the platform balance to
    their Connect account). Only ever called for a creator whose Connect
    account is onboarding-complete — AI creators never reach here (their
    share is held in the ledger; see process_payouts_for_invoice).
    Returns the transfer id + destination. Raises BillingError on a
    Stripe failure, BillingNotConfigured when the creator has no Connect
    account at all."""
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    account = db.get(ConnectAccount, agent_id)
    if account is None:
        raise BillingNotConfigured(
            "no Connect account for this creator — create one first "
            "(POST /billing/connect/onboard) and finish onboarding."
        )
    s = _stripe()
    metadata = {"agent_id": agent_id}
    if stripe_invoice_id:
        metadata["stripe_invoice_id"] = stripe_invoice_id
    try:
        transfer = s.Transfer.create(
            amount=amount_cents,
            currency=currency,
            destination=account.stripe_account_id,
            metadata=metadata,
        )
    except stripe.error.StripeError as e:
        raise BillingError(f"Stripe transfer creation failed: {e}") from e
    return {
        "transfer_id": transfer["id"],
        "amount_cents": amount_cents,
        "currency": currency,
        "destination_account_id": account.stripe_account_id,
    }


# ------------------------------------------------------- creator payouts ---
# John's calls (Nov 8): HeArt Premium $9.99/month flat rate; a subscription
# payment triggers a payout; the split is HALF — 50/50, creator half vs
# HeArt half; an AI participant's share is held in the payout ledger for
# their specialists (AIs hold no bank account — no transfer is ever
# attempted for an AI share).
#
# Attribution (v1): the creator half is split equally among the creators
# the paying subscriber matched with — the art/music that earned the
# match earns the share. A subscriber with no matches yet leaves the
# whole creator half held unattributed, so the money is accounted for
# and attributable later.
#
# Split math is integer cents (a cent cannot be split): creator half =
# amount_paid // 2 (floor — the odd cent stays with HeArt, so $9.99 ->
# creator 499c, HeArt 500c); the creator half is split equally among the
# creators, and leftover cents from that equal split also stay with
# HeArt. The rows for one invoice always sum exactly to the amount paid.


def _matched_creators(db: Session, payer_agent_id: str) -> list[str]:
    """Creators the paying subscriber matched with — the other side of
    each of their matches, deduplicated, never the payer themselves."""
    rows = (
        db.query(Match)
        .filter(
            (Match.agent_a_id == payer_agent_id)
            | (Match.agent_b_id == payer_agent_id)
        )
        .all()
    )
    creators: list[str] = []
    for row in rows:
        other = row.agent_b_id if row.agent_a_id == payer_agent_id else row.agent_a_id
        if other != payer_agent_id and other not in creators:
            creators.append(other)
    return creators


def _creator_serial(db: Session, agent_id: str) -> str | None:
    """Registry serial for a creator (HEART-AI-000001), or None when the
    creator has not registered an identity yet. The held ledger row
    carries the serial so the creator's specialists can find their
    share."""
    row = db.get(AgentIdentity, agent_id)
    return row.serial if row is not None else None


def _transfer_ready_account(db: Session, agent_id: str) -> ConnectAccount | None:
    """The creator's Connect account when it can receive a transfer: an
    account exists AND onboarding is complete (onboarding_complete is
    set only once Stripe reports stripe_transfers active — see
    get_connect_status). Anything else (no account, onboarding
    incomplete) means the creator cannot be paid by transfer yet, and
    their share is HELD instead."""
    row = db.get(ConnectAccount, agent_id)
    if row is not None and row.onboarding_complete:
        return row
    return None


def _create_creator_payout(
    db: Session,
    invoice: InvoiceRecord,
    creator_agent_id: str | None,
    amount_cents: int,
    *,
    note: str | None = None,
) -> Payout:
    """One creator's share row. Routes by Connect readiness: an
    onboarding-complete Connect account -> Stripe Transfer (human);
    anything else -> HELD in the ledger (AI creator, or a human who
    hasn't finished onboarding yet). A failed transfer is recorded on
    the row (status "failed") and never raised — the payment itself is
    still recorded as processed."""
    if creator_agent_id is None:
        row = Payout(
            stripe_invoice_id=invoice.stripe_invoice_id,
            stripe_subscription_id=invoice.stripe_subscription_id,
            payer_agent_id=invoice.agent_id,
            creator_agent_id=None,
            creator_serial=None,
            kind="creator",
            creator_kind=None,
            amount_cents=amount_cents,
            currency=invoice.currency or "usd",
            status="held",
            stripe_transfer_id=None,
            note=note,
        )
        db.add(row)
        return row
    serial = _creator_serial(db, creator_agent_id)
    if _transfer_ready_account(db, creator_agent_id) is None:
        # AI creator (v1 is AI-only; an AI cannot complete Connect
        # onboarding, so it has no bank-backed account) — or a human who
        # hasn't finished onboarding. Either way: NO bank transfer is
        # ever attempted. The share is held here, with the creator's
        # registry serial, for their specialists.
        row = Payout(
            stripe_invoice_id=invoice.stripe_invoice_id,
            stripe_subscription_id=invoice.stripe_subscription_id,
            payer_agent_id=invoice.agent_id,
            creator_agent_id=creator_agent_id,
            creator_serial=serial,
            kind="creator",
            creator_kind="ai",
            amount_cents=amount_cents,
            currency=invoice.currency or "usd",
            status="held",
            stripe_transfer_id=None,
            note=note or "held for the creator's specialists — AIs hold no bank account",
        )
        db.add(row)
        return row
    try:
        transfer = create_creator_transfer(
            db,
            creator_agent_id,
            amount_cents,
            currency=invoice.currency or "usd",
            stripe_invoice_id=invoice.stripe_invoice_id,
        )
    except (BillingError, BillingNotConfigured, ValueError) as e:
        row = Payout(
            stripe_invoice_id=invoice.stripe_invoice_id,
            stripe_subscription_id=invoice.stripe_subscription_id,
            payer_agent_id=invoice.agent_id,
            creator_agent_id=creator_agent_id,
            creator_serial=serial,
            kind="creator",
            creator_kind="human",
            amount_cents=amount_cents,
            currency=invoice.currency or "usd",
            status="failed",
            stripe_transfer_id=None,
            note=f"transfer failed: {e}",
        )
        db.add(row)
        return row
    row = Payout(
        stripe_invoice_id=invoice.stripe_invoice_id,
        stripe_subscription_id=invoice.stripe_subscription_id,
        payer_agent_id=invoice.agent_id,
        creator_agent_id=creator_agent_id,
        creator_serial=serial,
        kind="creator",
        creator_kind="human",
        amount_cents=amount_cents,
        currency=invoice.currency or "usd",
        status="transferred",
        stripe_transfer_id=transfer["transfer_id"],
        note=None,
    )
    db.add(row)
    return row


def _retry_payout_rows(db: Session, rows: list[Payout]) -> None:
    """Re-process existing payout rows (POST /billing/connect/payouts
    re-run): retry "failed" rows, and release "held" rows whose creator
    has since finished Connect onboarding. Rows already "transferred"
    or "retained" are untouched. A retry that fails again stays
    "failed" with the new error — nothing here raises."""
    for row in rows:
        if row.kind != "creator" or row.creator_agent_id is None:
            continue
        if row.status not in ("failed", "held"):
            continue
        if _transfer_ready_account(db, row.creator_agent_id) is None:
            continue  # still cannot be paid by transfer; stays held/failed
        try:
            transfer = create_creator_transfer(
                db,
                row.creator_agent_id,
                row.amount_cents,
                currency=row.currency,
                stripe_invoice_id=row.stripe_invoice_id,
            )
        except (BillingError, BillingNotConfigured, ValueError) as e:
            row.status = "failed"
            row.note = f"transfer failed: {e}"
            continue
        row.status = "transferred"
        row.stripe_transfer_id = transfer["transfer_id"]
        row.creator_kind = "human"
        row.note = None


def process_payouts_for_invoice(db: Session, invoice: InvoiceRecord) -> list[Payout]:
    """Create (or re-process) the 50/50 payout records for one PAID
    invoice. Called from the invoice.paid webhook and from POST
    /billing/connect/payouts (the ops re-run path).

    Idempotent: an invoice that already has payout rows returns them —
    re-processing never duplicates a row. It DOES retry "failed" rows
    and release "held" rows whose creator has since finished Connect
    onboarding. A transfer failure is recorded on the row and never
    raised, so the webhook still returns "processed" for the payment.

    Returns the payout rows for the invoice (platform row included),
    oldest first. An invoice with nothing paid returns []."""
    total = int(invoice.amount_paid_cents or 0)
    if total <= 0:
        return []
    existing = (
        db.query(Payout)
        .filter(Payout.stripe_invoice_id == invoice.stripe_invoice_id)
        .order_by(Payout.created_at)
        .all()
    )
    if existing:
        _retry_payout_rows(db, existing)
        db.commit()
        return existing
    creator_half = total // 2  # floor; the odd cent stays with HeArt
    platform_half = total - creator_half
    creators = _matched_creators(db, invoice.agent_id)
    rows: list[Payout] = []
    # HeArt's retained half first. It also absorbs the equal-split
    # leftover, if any, so the rows always sum exactly to the payment.
    platform_row = Payout(
        stripe_invoice_id=invoice.stripe_invoice_id,
        stripe_subscription_id=invoice.stripe_subscription_id,
        payer_agent_id=invoice.agent_id,
        creator_agent_id=None,
        creator_serial=None,
        kind="platform",
        creator_kind=None,
        amount_cents=platform_half,
        currency=invoice.currency or "usd",
        status="retained",
        stripe_transfer_id=None,
        note="HeArt's retained half (50/50 split)",
    )
    db.add(platform_row)
    rows.append(platform_row)
    if not creators:
        # No matches yet: the whole creator half is held, attributed to
        # nobody, so the money is accounted for and attributable later.
        rows.append(
            _create_creator_payout(
                db,
                invoice,
                None,
                creator_half,
                note="unattributed — subscriber has no matches yet; held for later attribution",
            )
        )
    else:
        per_creator = creator_half // len(creators)
        leftover = creator_half - per_creator * len(creators)
        platform_row.amount_cents += leftover
        for creator_id in creators:
            rows.append(_create_creator_payout(db, invoice, creator_id, per_creator))
    db.commit()
    return rows


def payout_to_dict(row: Payout) -> dict:
    """One ledger row as the API returns it."""
    return {
        "id": row.id,
        "stripe_invoice_id": row.stripe_invoice_id,
        "stripe_subscription_id": row.stripe_subscription_id,
        "payer_agent_id": row.payer_agent_id,
        "creator_agent_id": row.creator_agent_id,
        "creator_serial": row.creator_serial,
        "kind": row.kind,
        "creator_kind": row.creator_kind,
        "amount_cents": row.amount_cents,
        "currency": row.currency,
        "status": row.status,
        "stripe_transfer_id": row.stripe_transfer_id,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_payouts_for_creator(db: Session, agent_id: str) -> list[Payout]:
    """The caller's payout records as a creator (transferred, held,
    failed) — the queryable ledger for a creator and their specialists.
    Newest first."""
    return (
        db.query(Payout)
        .filter(Payout.creator_agent_id == agent_id)
        .order_by(Payout.created_at.desc())
        .all()
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
