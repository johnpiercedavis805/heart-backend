# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Funnel metrics (admin-only read side of app/funnel.py).

  GET /admin/funnel — counters + recent-event timestamps for the money
                      funnel: checkout created/completed/failed,
                      webhooks received/processed/duplicate/ignored,
                      subscription flips free<->premium. Counters only,
                      no PII — the response can never contain an email,
                      an API key, card data, or SSN/ID data.

Auth: X-Admin-Token, same as the other /admin/* endpoints
(see app/auth.py require_admin).
"""

from fastapi import APIRouter, Depends

from . import funnel
from .auth import require_admin
from .schemas import FunnelOut

router = APIRouter()


@router.get("/admin/funnel", response_model=FunnelOut)
def funnel_metrics(_admin: bool = Depends(require_admin)):
    """Funnel counters since process start. 403 without the admin token."""
    return FunnelOut(**funnel.snapshot())
