# HeArt v1 API — invented by John Davis.
"""Funnel observability: structured logging + in-process counters for
the money funnel (checkout -> webhook -> premium flip).

  checkout.created / checkout.failed    — POST /billing/checkout,
                                          POST /billing/payment-sheet,
                                          POST /paypal/subscribe
  checkout.completed                    — a checkout.session.completed
                                          webhook that processed
  webhook.received / processed /
  duplicate / ignored                   — Stripe + PayPal webhooks
  subscription.free_to_premium /
  subscription.premium_to_free          — the premium boundary crossing,
                                          Stripe or PayPal

Counters only — NEVER PII: no emails, no API keys, no card data, no
SSN/ID data, no agent ids. A counter key is just
"<provider>.<event>" and a recent event is just the key + an ISO
timestamp. The counters live in-process (a process restart resets
them); GET /admin/funnel (admin token only, see funnel_routes.py)
is the read side. Structured log lines go to the "heart.funnel"
logger so Render's log stream shows the same events.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger("heart.funnel")

# A subscription status counts as premium while the buyer holds a live
# subscription — active, trialing, or past_due (Smart Retries is still
# working it). Anything else (canceled, incomplete, unpaid, or no row)
# is free.
PREMIUM_STATUSES = {"active", "trialing", "past_due"}

_MAX_RECENT = 100

_COUNTERS: dict[str, int] = {}
_RECENT: deque = deque(maxlen=_MAX_RECENT)
_STARTED_AT = datetime.now(timezone.utc).isoformat()
_LOCK = threading.Lock()


def record(event: str, *, provider: str | None = None) -> None:
    """Count one funnel event and log it. `event` is a fixed name from
    the list in the module docstring; `provider` is "stripe" or
    "paypal" (None for provider-less events)."""
    key = f"{provider}.{event}" if provider else event
    with _LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0) + 1
        total = _COUNTERS[key]
        _RECENT.append({"event": key, "at": datetime.now(timezone.utc).isoformat()})
    logger.info("funnel event=%s count=%d", key, total)


def record_subscription_flip(
    old_status: str | None, new_status: str | None, *, provider: str
) -> None:
    """Record a premium-boundary crossing, if any. old_status None =
    no subscription row yet (free). Only the crossing counts — a
    status change that stays on one side of the boundary is silent."""
    was_premium = (old_status or "").lower() in PREMIUM_STATUSES
    is_premium = (new_status or "").lower() in PREMIUM_STATUSES
    if is_premium and not was_premium:
        record("subscription.free_to_premium", provider=provider)
    elif was_premium and not is_premium:
        record("subscription.premium_to_free", provider=provider)


def snapshot() -> dict:
    """Current counters + recent events (timestamps only, no PII)."""
    with _LOCK:
        return {
            "counters": dict(_COUNTERS),
            "recent": list(_RECENT),
            "started_at": _STARTED_AT,
        }


def reset() -> None:
    """Zero every counter and clear recent events. Tests only — the
    live service never calls this."""
    with _LOCK:
        _COUNTERS.clear()
        _RECENT.clear()
