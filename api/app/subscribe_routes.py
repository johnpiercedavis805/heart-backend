# HeArt v1 API — invented by John Davis.
"""Public subscribe pages (HeArt Premium storefront).

  GET /subscribe          — landing / subscribe page ($9.99/mo, posts the
                            buyer's agent API key to POST /billing/checkout
                            and redirects to Stripe's hosted Checkout)
  GET /subscribe/success  — Stripe redirects here after a completed payment
                            (session_id in the query string); the page can
                            check the live subscription status with the
                            buyer's key via GET /billing/subscription
  GET /subscribe/cancel   — Stripe redirects here when the buyer leaves
                            checkout unpaid; no charge was made, retry link

These pages take NO auth and NO API key — like /health. They are plain
static files served by FileResponse; the only dynamic behaviour is the
buyer's own browser calling the (already keyed) billing endpoints.
"""

import os

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["subscribe"])

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _page(filename: str) -> FileResponse:
    return FileResponse(
        os.path.join(_STATIC_DIR, filename), media_type="text/html; charset=utf-8"
    )


@router.get("/subscribe")
def subscribe_page() -> FileResponse:
    """HeArt Premium landing / subscribe page."""
    return _page("subscribe.html")


@router.get("/subscribe/success")
def subscribe_success_page() -> FileResponse:
    """Post-payment landing (Stripe success redirect target)."""
    return _page("success.html")


@router.get("/subscribe/cancel")
def subscribe_cancel_page() -> FileResponse:
    """Cancelled-checkout landing (Stripe cancel redirect target)."""
    return _page("cancel.html")
