#!/usr/bin/env python3
"""Create the HeArt Premium product + $9.99/month billing plan in PayPal
SANDBOX, and print the plan id to put in PAYPAL_PLAN_ID.

    cd ~/workspace/heart-backend/api
    export PAYPAL_CLIENT_ID=...   # sandbox REST app client id
    export PAYPAL_CLIENT_SECRET=...  # sandbox REST app client secret
    ../.venv/bin/python scripts/paypal_create_plan.py

Refuses to run against the live PayPal host — sandbox only, while
PAYPAL_TEST_MODE_ONLY is on (the default). The plan is created once;
re-running prints the existing plan id instead of duplicating it.
Amount is $9.99/month, confirmed by John Nov 8 (flat rate).

What the script does (PayPal Subscriptions API):
  1. POST /v1/catalogs/products — "HeArt Premium" (SERVICE, SOFTWARE).
  2. POST /v1/billing/plans — one REGULAR monthly billing cycle,
     $9.99 USD, infinite cycles (total_cycles 0), auto-bill
     outstanding, payment failure threshold 3.
  3. If PayPal returns the plan in CREATED status, activate it
     (POST /v1/billing/plans/{id}/activate) — newer API versions
     return ACTIVE directly.

After it runs, set PAYPAL_PLAN_ID in Render env vars (and locally),
then create the webhook in the PayPal Developer Dashboard
(Apps & Credentials -> your app -> Webhooks) pointing at
https://heart-api-ifsr.onrender.com/paypal/webhooks with the events
in docs/paypal-subscriptions.md, and set PAYPAL_WEBHOOK_ID.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import paypal  # noqa: E402
from app.config import settings  # noqa: E402

PRODUCT_NAME = "HeArt Premium"
PRODUCT_DESCRIPTION = (
    "HeArt Premium — the paid tier of the creativity-first dating app. "
    "Core matching stays free; Premium is a flat-rate monthly subscription."
)
PLAN_NAME = "HeArt Premium — $9.99/month"


def main() -> int:
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        print(
            "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set. Export "
            "sandbox REST app credentials first (PayPal Developer "
            "Dashboard -> Apps & Credentials).",
            file=sys.stderr,
        )
        return 2
    try:
        paypal._require_sandbox()
    except paypal.PayPalNotConfigured as e:
        print(f"REFUSING: {e}", file=sys.stderr)
        return 2

    # Reuse the product if it already exists (idempotent by name).
    products = paypal._paypal_request(
        "GET", "/v1/catalogs/products?page_size=20"
    )
    product = next(
        (p for p in products.get("products", []) if p.get("name") == PRODUCT_NAME),
        None,
    )
    if product is None:
        product = paypal._paypal_request(
            "POST",
            "/v1/catalogs/products",
            {
                "name": PRODUCT_NAME,
                "description": PRODUCT_DESCRIPTION,
                "type": "SERVICE",
                "category": "SOFTWARE",
            },
        )
        print(f"created product {product['id']} ({PRODUCT_NAME})")
    else:
        print(f"reusing product {product['id']} ({PRODUCT_NAME})")

    # Reuse the $9.99/month plan for this product, or create it.
    plans = paypal._paypal_request(
        "GET",
        f"/v1/billing/plans?product_id={product['id']}&page_size=20",
    )
    plan = next(
        (
            p
            for p in plans.get("plans", [])
            if p.get("name") == PLAN_NAME
            and (p.get("billing_cycles") or [{}])[0]
            .get("pricing_scheme", {})
            .get("fixed_price", {})
            .get("value")
            == "9.99"
        ),
        None,
    )
    if plan is None:
        plan = paypal._paypal_request(
            "POST",
            "/v1/billing/plans",
            {
                "product_id": product["id"],
                "name": PLAN_NAME,
                "description": PRODUCT_DESCRIPTION,
                "billing_cycles": [
                    {
                        "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                        "tenure_type": "REGULAR",
                        "sequence": 1,
                        "total_cycles": 0,  # infinite — renews until cancelled
                        "pricing_scheme": {
                            "fixed_price": {"value": "9.99", "currency_code": "USD"}
                        },
                    }
                ],
                "payment_preferences": {
                    "auto_bill_outstanding": True,
                    "setup_fee_failure_action": "CONTINUE",
                    "payment_failure_threshold": 3,
                },
            },
        )
        print(f"created plan {plan['id']} ($9.99/month)")
    else:
        print(f"reusing plan {plan['id']} ($9.99/month)")

    if (plan.get("status") or "").upper() == "CREATED":
        paypal._paypal_request(
            "POST", f"/v1/billing/plans/{plan['id']}/activate", {}
        )
        print(f"activated plan {plan['id']}")

    print(f"\nPAYPAL_PLAN_ID={plan['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
