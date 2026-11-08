# HeArt v1 API — invented by John Davis.
"""Crypto payment endpoints (John's calls, Nov 8: USDC via Stripe AND
Bitcoin via Coinbase).

  POST /crypto/bitcoin/charge       — one-time $9.99 Bitcoin charge (hosted checkout)
  GET  /crypto/bitcoin/charges      — the caller's Bitcoin charges + premium windows
  POST /crypto/webhooks/coinbase    — Coinbase webhook receiver (signature-verified; NO auth key)

Every endpoint except /crypto/webhooks/coinbase requires the caller's
API key (the same auth as the rest of v1). Bitcoin is one-time only:
there is no auto-renew — each month is a fresh charge the user pays by
hand. Only a verified charge:confirmed webhook activates premium and
triggers the 50/50 creator payout (never the redirect alone).
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from . import coinbase
from .config import settings
from .db import get_db
from .ratelimit import RateLimiter
from .routes import get_requester
from .schemas import (
    BitcoinChargeIn,
    BitcoinChargeOut,
    CoinbaseWebhookOut,
    CryptoPaymentOut,
)

router = APIRouter(prefix="/crypto", tags=["crypto"])
limiter = RateLimiter(settings.RATE_LIMIT_PER_HOUR, 3600)


def _check_rate_limit(key) -> None:
    ok, retry_after = limiter.check(key.key_hash)
    if not ok:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit exceeded: {settings.RATE_LIMIT_PER_HOUR} requests/hour per key",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def _crypto_error(e: Exception) -> HTTPException:
    if isinstance(e, coinbase.CoinbaseNotConfigured):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    if isinstance(e, coinbase.CoinbaseError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    if isinstance(e, ValueError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    raise e


@router.post("/bitcoin/charge", response_model=BitcoinChargeOut, status_code=status.HTTP_201_CREATED)
def create_bitcoin_charge(
    body: BitcoinChargeIn = BitcoinChargeIn(),
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """Create a one-time $9.99 Bitcoin charge. Returns Coinbase's hosted
    checkout URL — the app opens it and the payer picks Bitcoin (or any
    coin Coinbase offers) and pays the BTC equivalent at the current
    rate. The charge expires after 1 hour if unpaid; premium activates
    only when the charge:confirmed webhook arrives. Returns 503 while
    Coinbase is not configured (no API key)."""
    agent, key = requester
    _check_rate_limit(key)
    try:
        return BitcoinChargeOut(
            **coinbase.create_btc_charge(
                db,
                agent.id,
                redirect_url=body.redirect_url,
                cancel_url=body.cancel_url,
            )
        )
    except Exception as e:
        raise _crypto_error(e)


@router.get("/bitcoin/charges", response_model=list[CryptoPaymentOut])
def list_bitcoin_charges(
    requester=Depends(get_requester),
    db: Session = Depends(get_db),
):
    """The caller's Bitcoin charges, newest first — status, what was
    paid, and the premium window (premium_until) for confirmed ones."""
    agent, key = requester
    _check_rate_limit(key)
    rows = coinbase.list_crypto_payments(db, agent.id)
    return [CryptoPaymentOut(**coinbase.crypto_payment_to_dict(r)) for r in rows]


@router.post("/webhooks/coinbase", response_model=CoinbaseWebhookOut)
async def coinbase_webhook(request: Request, db: Session = Depends(get_db)):
    """Coinbase webhook receiver. NO API-key auth — Coinbase calls this,
    so the signature IS the auth: the raw body is verified against
    COINBASE_WEBHOOK_SECRET (HMAC-SHA256, X-CC-Webhook-Signature
    header) before anything is processed. Bad signature or missing
    secret -> 400/503, nothing processed. Only charge:confirmed
    activates premium and triggers the 50/50 payout; re-deliveries of
    an already confirmed charge are duplicates and change nothing."""
    payload = await request.body()
    sig_header = request.headers.get("x-cc-webhook-signature")
    try:
        coinbase.verify_coinbase_webhook(payload, sig_header)
    except coinbase.CoinbaseNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except ValueError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"webhook signature verification failed: {e}"
        )
    try:
        event = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"webhook payload is not valid JSON: {e}"
        )
    try:
        result = coinbase.handle_coinbase_webhook(db, event)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"webhook handling failed (Coinbase will retry): {e}",
        )
    return CoinbaseWebhookOut(received=True, result=result)
