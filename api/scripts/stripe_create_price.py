#!/usr/bin/env python3
"""Create the HeArt Premium product + monthly flat-rate prices in Stripe
TEST MODE, and print the price ids to put in STRIPE_PRICE_PREMIUM_MONTHLY
(card, USD) and STRIPE_PRICE_PREMIUM_MONTHLY_USDC (USDC).

    cd ~/workspace/heart-backend/api
    export STRIPE_SECRET_KEY=sk_test_...
    ../.venv/bin/python scripts/stripe_create_price.py

Refuses to run with a live key (sk_live_...) — HeArt takes no payments
yet, and the first live charge is a separate decision for John. Prices
are created once; re-running prints the existing price ids instead of
duplicating them. Amount is $9.99/month, confirmed by John Nov 8.

USDC units: USDC has 6 decimals in Stripe's API, so 9.99 USDC =
9,990,000 smallest units. If Stripe ever reports USDC amounts in
different units, USDC_AMOUNT_CENTS is the one number to fix — and the
webhook normalization in billing._usd_cents must match it.
"""

import os
import sys

AMOUNT_CENTS = 999  # $9.99/month — confirmed by John Nov 8 (flat rate)
CURRENCY = "usd"
USDC_AMOUNT = 9_990_000  # 9.99 USDC in smallest units (6 decimals)
USDC_CURRENCY = "usdc"
PRODUCT_NAME = "HeArt Premium"
PRODUCT_DESCRIPTION = (
    "HeArt Premium — the paid tier of the creativity-first dating app. "
    "Core matching stays free; Premium is a flat-rate monthly subscription."
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import stripe  # noqa: E402

TEST_KEY_PREFIX = "sk_test_"


def _get_or_create_price(product_id: str, unit_amount: int, currency: str) -> object:
    """Reuse the matching price for this product, or create it. A price
    is identified by (unit_amount, currency) — the USD and USDC prices
    are two different prices on the same product."""
    prices = stripe.Price.list(product=product_id, limit=100)
    price = next(
        (p for p in prices.data if p.unit_amount == unit_amount and p.currency == currency),
        None,
    )
    if price is None:
        price = stripe.Price.create(
            product=product_id,
            unit_amount=unit_amount,
            currency=currency,
            recurring={"interval": "month"},
        )
        print(f"created price {price.id} ({unit_amount} {currency}/month)")
    else:
        print(f"reusing price {price.id} ({unit_amount} {currency}/month)")
    return price


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

    usd_price = _get_or_create_price(product.id, AMOUNT_CENTS, CURRENCY)
    usdc_price = _get_or_create_price(product.id, USDC_AMOUNT, USDC_CURRENCY)

    print()
    print("Set these in your environment (and in the deployment's env vars):")
    print(f"  STRIPE_PRICE_PREMIUM_MONTHLY={usd_price.id}")
    print(f"  STRIPE_PRICE_PREMIUM_MONTHLY_USDC={usdc_price.id}")
    print(f"  Amount: {AMOUNT_CENTS / 100:.2f} {CURRENCY.upper()}/month, flat rate, one price per user")
    print(f"  Amount: 9.99 USDC/month (same price, stablecoin — settles in USD)")
    print()
    print("USDC note: the USDC price only works once stablecoins are enabled")
    print("in the Stripe Dashboard (Settings -> Payment methods) and the")
    print("stablecoin-subscription preview is granted — see docs/crypto-payments.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
