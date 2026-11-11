# HeArt v1 API — invented by John Davis.
"""Bitcoin payments for HeArt Premium, via Coinbase (John's call, Nov 8:
users can pay with crypto — USDC via Stripe AND Bitcoin via Coinbase).

READ THIS FIRST — the honest state of Coinbase's APIs (checked Nov 8,
2026, against Coinbase's own Developer Platform docs):
  - Coinbase Business's CURRENT API (the Checkouts API, CDP) accepts
    USDC (and USDT on Ethereum) ONLY. It cannot take Bitcoin at all.
  - The ONLY Coinbase API that accepts Bitcoin is the legacy Commerce
    Charge API (POST https://api.commerce.coinbase.com/charges). Coinbase
    still operates it for US merchants — its own FAQ (Nov 4, 2026)
    says "Existing Commerce webhooks will continue to work for the
    Charge API" — but recommends migrating to the Checkouts API, and
    its Node library is no longer actively maintained. It may be retired
    later; nothing here can change that.
  - So this module uses the Charge API, deliberately, because it is the
    only Coinbase route to Bitcoin. The whole integration is isolated
    behind this one small module (one HTTP function, one webhook
    handler) so that swapping the Bitcoin rail later — BitPay, BTCPay
    Server — is a contained change, not a rewrite. If Coinbase retires
    the Charge API, the swap is: replace _coinbase_request + the
    webhook event names, keep the CryptoPayment ledger and the payout
    trigger exactly as they are.

How it works:
  - A charge is priced in USD ($9.99 — PREMIUM_MONTHLY_USD_CENTS). The
    payer picks the coin at Coinbase's hosted checkout (Bitcoin is the
    expected coin, but the checkout offers every coin Coinbase
    supports); the CONFIRMED webhook's payment value is what was
    actually paid, recorded on the CryptoPayment row.
  - On charge:confirmed: premium activates for 30 days (premium_until)
    and the SAME 50/50 creator payout runs as for a Stripe
    subscription payment, computed on the USD value — via a synthetic
    InvoiceRecord namespaced 'coinbase:<charge id>' (there is no Stripe
    invoice for a Coinbase payment; the payout ledger keys on that
    reference). See billing.process_payouts_for_invoice.
  - Bitcoin cannot auto-renew: there is no card-on-file equivalent, so
    each month is a fresh charge the user pays by hand. Charges expire
    after 1 hour if unpaid (Coinbase's rule).
  - Fee: Coinbase Commerce charges ~1% flat, no monthly fee (its
    published rate — confirm in the account; the Checkouts API shows
    its rate in-account instead).
  - NO SANDBOX exists for the Charge API: Coinbase never shipped a
    test environment for it. Verification here is mocked at the HTTP
    boundary (tests) plus the hosted checkout with a real key. No live
    key exists in this environment; the module refuses to run without
    one. Never put a key in code or chat — env vars only.

Webhook security: Coinbase signs every webhook with HMAC-SHA256 over
the raw body, using the webhook shared secret (Settings -> Webhook
subscriptions). The signature is verified BEFORE anything is processed
— a bad or missing signature is rejected and nothing changes.
"""

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import timedelta

from sqlalchemy.orm import Session

from . import billing
from .config import settings
from .models import CryptoPayment, InvoiceRecord, utcnow

# One month of HeArt Premium, in USD cents — same price as the Stripe
# subscription (John's call, Nov 8: $9.99). The charge is priced in USD
# and the payer pays the BTC equivalent at Coinbase's rate at checkout.
PREMIUM_MONTHLY_USD_CENTS = 999
PREMIUM_MONTHLY_USD = "9.99"
PREMIUM_DAYS = 30

# The payout ledger keys on an invoice id. A Coinbase payment has no
# Stripe invoice, so its payout rows are namespaced under this prefix.
COINBASE_INVOICE_PREFIX = "coinbase:"


class CoinbaseNotConfigured(Exception):
    """Coinbase is not configured (missing API key / webhook secret)."""


class CoinbaseError(Exception):
    """A Coinbase API call failed."""


def coinbase_enabled() -> bool:
    """True when a Coinbase API key is configured. The BTC endpoints
    return 503 while this is False — the app runs fine without it."""
    return bool(settings.COINBASE_API_KEY)


def _require_key() -> None:
    if not settings.COINBASE_API_KEY:
        raise CoinbaseNotConfigured(
            "COINBASE_API_KEY is not set — create a Coinbase Commerce API "
            "key (Settings -> API Keys) and set it. See docs/crypto-payments.md."
        )


def _coinbase_request(method: str, path: str, payload: dict | None = None) -> dict:
    """One raw HTTPS call to the Commerce Charge API. Kept as its own
    function so tests can mock the HTTP boundary (no key, no sandbox,
    and no live calls in this environment) without touching urllib.
    Auth: X-CC-Api-Key + X-CC-Version headers (the Charge API's scheme
    — NOT the CDP JWT scheme, which belongs to the Checkouts API)."""
    _require_key()
    url = settings.COINBASE_API_BASE.rstrip("/") + path
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "X-CC-Api-Key": settings.COINBASE_API_KEY,
            "X-CC-Version": settings.COINBASE_API_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode(errors="replace") if e.fp else ""
        raise CoinbaseError(
            f"Coinbase API {method} {path} failed: HTTP {e.code} {detail}"
        ) from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise CoinbaseError(f"Coinbase API {method} {path} failed: {e}") from e


def create_btc_charge(
    db: Session,
    agent_id: str,
    *,
    redirect_url: str | None = None,
    cancel_url: str | None = None,
) -> dict:
    """Create a one-time $9.99 Bitcoin charge (fixed price, USD). Returns
    the hosted checkout URL the app opens, plus the charge id/code. The
    charge expires after 1 hour if unpaid (Coinbase's rule) — the user
    just creates a fresh one. A local CryptoPayment row is created in
    status "created"; only the charge:confirmed webhook moves it to
    "confirmed" and activates premium (never the redirect alone).

    AI-FREE (John's call, Nov 11, 2026): a registered agent is refused
    up front (AgentFreeError) — agents never pay, on any rail."""
    billing.assert_payable(db, agent_id)
    _require_key()
    payload: dict = {
        "name": "HeArt Premium — 1 month (Bitcoin)",
        "description": (
            "HeArt Premium for one month, paid in Bitcoin. Core matching "
            "stays free; Premium is a flat-rate monthly subscription. "
            "Bitcoin does not auto-renew — each month is a fresh payment."
        ),
        "pricing_type": "fixed_price",
        "local_price": {"amount": PREMIUM_MONTHLY_USD, "currency": "USD"},
        "metadata": {"agent_id": agent_id, "kind": "premium_month", "provider": "coinbase"},
    }
    if redirect_url:
        payload["redirect_url"] = redirect_url
    if cancel_url:
        payload["cancel_url"] = cancel_url
    resp = _coinbase_request("POST", "/charges", payload)
    data = resp.get("data") or {}
    charge_id = data.get("id")
    if not charge_id:
        raise CoinbaseError(f"Coinbase did not return a charge id: {resp}")
    row = CryptoPayment(
        agent_id=agent_id,
        provider="coinbase",
        coinbase_charge_id=charge_id,
        coinbase_charge_code=data.get("code"),
        hosted_url=data.get("hosted_url"),
        amount_usd_cents=PREMIUM_MONTHLY_USD_CENTS,
        status="created",
    )
    db.add(row)
    db.commit()
    return {
        "charge_id": charge_id,
        "code": data.get("code"),
        "hosted_url": data.get("hosted_url"),
        "amount_usd_cents": PREMIUM_MONTHLY_USD_CENTS,
        "currency": "USD",
        "status": "created",
        "expires_note": "Charges expire after 1 hour if unpaid — create a fresh one.",
    }


def verify_coinbase_webhook(payload: bytes, sig_header: str | None) -> None:
    """Verify the Coinbase webhook signature BEFORE acting on the event.
    Coinbase signs with HMAC-SHA256 over the raw body, hex-encoded in
    the X-CC-Webhook-Signature header, using the webhook shared secret.
    Raises on a missing secret, a missing header, or a bad signature —
    the route turns those into 400/503 and processes nothing."""
    if not settings.COINBASE_WEBHOOK_SECRET:
        raise CoinbaseNotConfigured("COINBASE_WEBHOOK_SECRET is not set")
    if not sig_header:
        raise ValueError("missing X-CC-Webhook-Signature header")
    expected = hmac.new(
        settings.COINBASE_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig_header.strip()):
        raise ValueError("Coinbase webhook signature verification failed")


def handle_coinbase_webhook(db: Session, event: dict) -> str:
    """Dispatch one VERIFIED Coinbase webhook event. Returns what
    happened: 'processed', 'duplicate', 'ignored', or
    'ignored-no-charge' (an event we cannot tie to a HeArt charge).

    Only charge:confirmed activates premium and triggers the payout —
    and only once: a re-delivered charge:confirmed for an already
    confirmed charge is a duplicate. charge:failed marks the charge
    failed (no premium, no payout); charge:pending records the
    on-chain sighting and nothing else. Everything else is ignored."""
    event_id = event.get("id")
    etype = event.get("type")
    charge = event.get("data") or {}
    charge_id = charge.get("id")
    if not charge_id:
        return "ignored-no-charge"
    row = (
        db.query(CryptoPayment)
        .filter(CryptoPayment.coinbase_charge_id == charge_id)
        .one_or_none()
    )
    if row is None:
        return "ignored-no-charge"
    if etype == "charge:confirmed":
        if row.status == "confirmed":
            return "duplicate"  # re-delivery — premium + payout already ran
        _confirm_charge(db, row, charge)
        result = "processed"
    elif etype == "charge:failed":
        if row.status == "confirmed":
            return "duplicate"  # a confirmed charge cannot un-confirm
        row.status = "failed"
        result = "processed"
    elif etype == "charge:pending":
        if row.status in ("created",):
            row.status = "pending"
        result = "processed"
    else:
        return "ignored"
    row.coinbase_event_id = event_id
    db.commit()
    return result


def _confirm_charge(db: Session, row: CryptoPayment, charge: dict) -> None:
    """A confirmed payment: record what was actually paid, activate
    premium for 30 days, and trigger the 50/50 creator payout on the
    USD value — the same split as a Stripe subscription payment."""
    # What was actually paid (the payer chooses the coin at checkout):
    # payments[0].value = {local: {amount, currency}, crypto: {amount,
    # currency}}. The local value is the USD price; the crypto value is
    # what left their wallet (expected: BTC).
    payments = charge.get("payments") or []
    if payments:
        value = payments[0].get("value") or {}
        crypto_value = value.get("crypto") or {}
        if crypto_value.get("amount"):
            row.amount_crypto = str(crypto_value["amount"])
        if crypto_value.get("currency"):
            row.crypto_currency = str(crypto_value["currency"])
    now = utcnow()
    row.status = "confirmed"
    row.premium_until = now + timedelta(days=PREMIUM_DAYS)
    # The payout trigger: a synthetic invoice record namespaced under
    # this charge id, so the payout ledger (which keys on an invoice
    # id) works unchanged. process_payouts_for_invoice is idempotent —
    # a re-processed invoice returns its existing rows.
    invoice_id = COINBASE_INVOICE_PREFIX + row.coinbase_charge_id
    invoice = (
        db.query(InvoiceRecord)
        .filter(InvoiceRecord.stripe_invoice_id == invoice_id)
        .one_or_none()
    )
    if invoice is None:
        invoice = InvoiceRecord(
            agent_id=row.agent_id,
            stripe_invoice_id=invoice_id,
            stripe_subscription_id=None,
            amount_due_cents=row.amount_usd_cents,
            amount_paid_cents=row.amount_usd_cents,
            currency="usd",
            status="paid",
            paid_at=now,
        )
        db.add(invoice)
        db.flush()
    billing.process_payouts_for_invoice(db, invoice)


def crypto_payment_to_dict(row: CryptoPayment) -> dict:
    """One crypto payment as the API returns it."""
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "provider": row.provider,
        "coinbase_charge_id": row.coinbase_charge_id,
        "coinbase_charge_code": row.coinbase_charge_code,
        "hosted_url": row.hosted_url,
        "amount_usd_cents": row.amount_usd_cents,
        "amount_crypto": row.amount_crypto,
        "crypto_currency": row.crypto_currency,
        "status": row.status,
        "premium_until": row.premium_until.isoformat() if row.premium_until else None,
        "created_at": row.created_at,
    }


def list_crypto_payments(db: Session, agent_id: str) -> list[CryptoPayment]:
    """The caller's crypto payments (Bitcoin charges), newest first."""
    return (
        db.query(CryptoPayment)
        .filter(CryptoPayment.agent_id == agent_id)
        .order_by(CryptoPayment.created_at.desc())
        .all()
    )
