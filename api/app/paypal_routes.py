# HeArt v1 API — invented by John Davis.
"""PayPal subscription endpoints (John's call, Nov 9, 2026: cards +
PayPal is enough).

  POST /paypal/subscribe       — create a $9.99/month PayPal subscription (approval URL)
  GET  /paypal/subscriptions   — the caller's PayPal subscriptions
  POST /paypal/webhooks        — PayPal webhook receiver (signature-verified; NO auth key)

Every endpoint except /paypal/webhooks requires the caller's API key
(the same auth as the rest of v1). PayPal is a SEPARATE rail from
Stripe: Stripe-native PayPal is Europe-only and this is a US account,
so PayPal's own Subscriptions API handles the recurring billing (see
app/paypal.py). Only a verified BILLING.SUBSCRIPTION.ACTIVATED
webhook activates premium, and only a verified PAYMENT.SALE.COMPLETED
triggers the 50/50 creator payout — never the redirect back from
PayPal alone. PayPal payments are ledger-mirrored only: no PayPal
Payouts are made (moving money out to creators is a follow-up).
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from . import funnel, paypal
from .config import settings
from .db import get_db
from .ratelimit import RateLimiter
from .routes import get_requester
from .schemas import (
    PayPalSubscribeIn,
    PayPalSubscribeOut,
    PayPalSubscriptionOut,
    PayPalWebhookOut,
)

router = APIRouter(prefix="/paypal", tags=["paypal"])
limiter = RateLimiter(settings.RATE_LIMIT_PER_HOUR, 3600)


def _check_rate_limit(key) -> None:
    ok, retry_after = limiter.check(key.key_hash)
    if not ok:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit exceeded: {settings.RATE_LIMIT_PER_HOUR} requests/hour per key",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def _paypal_error(e: Exception) -> HTTPException:
    if isinstance(e, paypal.PayPalNotConfigured):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    if isinstance(e, paypal.PayPalError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    if isinstance(e, ValueError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    raise e


@router.post("/subscribe", response_model=PayPalSubscribeOut, status_code=status.HTTP_201_CREATED)
def create_subscription(
    body: PayPalSubscribeIn = PayPalSubscribeIn(),
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Create a $9.99/month PayPal subscription. Returns the approval
    URL — the app opens it and the buyer approves in PayPal. The
    subscription starts APPROVAL_PENDING; premium activates only when
    the BILLING.SUBSCRIPTION.ACTIVATED webhook arrives. Returns 503
    while PayPal is not configured (no client id/secret or plan id).
    An agent already premium via Stripe gets a 502 (no double
    billing); an agent already ACTIVE on PayPal gets their existing
    subscription back with approval_url null."""
    agent, key = requester
    _check_rate_limit(key)
    try:
        out = PayPalSubscribeOut(
            **paypal.create_paypal_subscription(
                db,
                agent.id,
                return_url=body.return_url,
                cancel_url=body.cancel_url,
            )
        )
    except Exception as e:
        funnel.record("checkout.failed", provider="paypal")
        raise _paypal_error(e)
    funnel.record("checkout.created", provider="paypal")
    return out


@router.get("/subscriptions", response_model=list[PayPalSubscriptionOut])
def list_subscriptions(
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """The caller's PayPal subscriptions, newest first — status and
    next billing time for each."""
    agent, key = requester
    _check_rate_limit(key)
    rows = paypal.list_paypal_subscriptions(db, agent.id)
    return [PayPalSubscriptionOut(**paypal.paypal_subscription_to_dict(r)) for r in rows]


@router.post("/webhooks", response_model=PayPalWebhookOut)
async def paypal_webhook(request: Request, db: Session = Depends(get_db)):
    """PayPal webhook receiver. NO API-key auth — PayPal calls this, so
    the signature IS the auth: the raw body + the five transmission
    headers are verified with PayPal's verify-webhook-signature
    endpoint before anything is processed. Bad verification or a
    missing webhook id -> 400/503, nothing processed. Only
    BILLING.SUBSCRIPTION.ACTIVATED activates premium; only
    PAYMENT.SALE.COMPLETED triggers the 50/50 payout; re-deliveries
    are duplicates and change nothing."""
    payload = await request.body()
    headers = {
        "paypal-auth-algo": request.headers.get("paypal-auth-algo"),
        "paypal-cert-url": request.headers.get("paypal-cert-url"),
        "paypal-transmission-id": request.headers.get("paypal-transmission-id"),
        "paypal-transmission-sig": request.headers.get("paypal-transmission-sig"),
        "paypal-transmission-time": request.headers.get("paypal-transmission-time"),
    }
    try:
        verified = paypal.verify_paypal_webhook(payload, headers)
    except paypal.PayPalNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except ValueError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"webhook verification failed: {e}"
        )
    if not verified:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "webhook signature verification failed: PayPal did not confirm this event",
        )
    try:
        event = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"webhook payload is not valid JSON: {e}"
        )
    funnel.record("webhook.received", provider="paypal")
    try:
        result = paypal.handle_paypal_webhook(db, event)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"webhook handling failed (PayPal will retry): {e}",
        )
    if result == "processed":
        funnel.record("webhook.processed", provider="paypal")
    elif result == "duplicate":
        funnel.record("webhook.duplicate", provider="paypal")
    elif result.startswith("ignored"):
        funnel.record("webhook.ignored", provider="paypal")
    return PayPalWebhookOut(received=True, result=result)
