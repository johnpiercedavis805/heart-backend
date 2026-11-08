#!/usr/bin/env python3
"""Create the HeArt Premium product + monthly flat-rate price in Stripe
TEST MODE, and print the price id to put in STRIPE_PRICE_PREMIUM_MONTHLY.

    cd ~/workspace/heart-backend/api
    export STRIPE_SECRET_KEY=sk_test_...
    ../.venv/bin/python scripts/stripe_create_price.py

Refuses to run with a live key (sk_live_...) — HeArt takes no payments
yet, and the first live charge is a separate decision for John. The
price is created once; re-running prints the existing price id instead
of duplicating it. Amount is $9.99/month, confirmed by John Nov 8.
"""

import os
import sys

AMOUNT_CENTS = 999  # $9.99/month — confirmed by John Nov 8 (flat rate)
CURRENCY = "usd"
PRODUCT_NAME = "HeArt Premium"
PRODUCT_DESCRIPTION = (
    "HeArt Premium — the paid tier of the creativity-first dating app. "
    "Core matching stays free; Premium is a flat-rate monthly subscription."
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import stripe  # noqa: E402

TEST_KEY_PREFIX = "sk_test_"


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        print("STRIPE_SECRET_KEY is not set. Export a TEST key (sk_test_...) first.", file=sys.stderr)
        return 2
    if not key.startswith(TEST_KEY_PREFIX):
        print(
            "REFUSING: STRIPE_SECRET_KEY does not start with sk_test_. "
            "Live keys are never used by this script — HeArt takes no payments yet.",
            file=sys.stderr,
        )
        return 2
    stripe.api_key = key

    # Reuse the product if it already exists (idempotent by name).
    products = stripe.Product.list(limit=100)
    product = next((p for p in products.data if p.name == PRODUCT_NAME), None)
    if product is None:
        product = stripe.Product.create(name=PRODUCT_NAME, description=PRODUCT_DESCRIPTION)
        print(f"created product {product.id}")
    else:
        print(f"reusing product {product.id}")

    prices = stripe.Price.list(product=product.id, limit=100)
    price = next(
        (p for p in prices.data if p.unit_amount == AMOUNT_CENTS and p.currency == CURRENCY),
        None,
    )
    if price is None:
        price = stripe.Price.create(
            product=product.id,
            unit_amount=AMOUNT_CENTS,
            currency=CURRENCY,
            recurring={"interval": "month"},
        )
        print(f"created price {price.id}")
    else:
        print(f"reusing price {price.id}")

    print()
    print("Set this in your environment (and in the deployment's env vars):")
    print(f"  STRIPE_PRICE_PREMIUM_MONTHLY={price.id}")
    print(f"  Amount: {AMOUNT_CENTS / 100:.2f} {CURRENCY.upper()}/month, flat rate, one price per user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
