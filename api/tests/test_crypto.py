# HeArt v1 API — invented by John Davis.
"""End-to-end verification of crypto payments for HeArt Premium (John's
calls, Nov 8 — USDC via Stripe AND Bitcoin via Coinbase), run with the
venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_crypto.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  USDC price creation (scripts/stripe_create_price.py: 9.99 USDC =
  9,990,000 smallest units) ->
  hosted Checkout + Payment Sheet with currency="usdc" (USDC price
  selected; default stays USD; invalid currency rejected) ->
  USDC invoice.paid webhook: micro-USDC amounts normalize to USD cents
  (9,990,000 -> 999) and flow through the SAME 50/50 payout path as a
  card payment ->
  Bitcoin charge creation (Coinbase Commerce Charge API, mocked at the
  HTTP boundary) ->
  Coinbase webhooks: REAL HMAC-SHA256 signature verification (bad /
  missing signature -> 400); charge:confirmed activates one month of
  premium and triggers the 50/50 split on the USD value; charge:failed
  and charge:pending do neither; re-delivery is a duplicate ->
  model/migration agreement (migration 14, Nov 10, 2026):
  crypto_payments id is uuid in both the model (UuidString) and the
  migrations, with a PK re-read test on the charge:confirmed path that
  500'd on Postgres with a text id.

Coinbase API calls are mocked at the HTTP boundary (app.coinbase.
_coinbase_request) — no key exists in this environment, there is no
sandbox for the Charge API, and no live calls are made. What IS verified
for real: the full HTTP stack, the exact request payloads, the local
mirrors and ledger rows, and webhook signature crypto.

KEY RULES UNDER TEST: USDC settles in USD, so the payout math is
unchanged (creator half = amount_paid // 2 in USD cents); Bitcoin
cannot auto-renew — each month is a fresh charge, premium is a 30-day
window, and only a verified charge:confirmed activates it; a failed or
unconfirmed payment activates nothing and pays nobody.
"""

import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_crypto_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py
os.environ["MAX_WORKS_PER_DAY"] = "10"  # test process only; production default stays 1/day
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake_key_for_unit_tests_only"
os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_fake_key_for_unit_tests_only"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret_for_unit_tests_only"
os.environ["STRIPE_PRICE_PREMIUM_MONTHLY"] = "price_test_premium"
os.environ["STRIPE_PRICE_PREMIUM_MONTHLY_USDC"] = "price_test_premium_usdc"
os.environ["STRIPE_CHECKOUT_SUCCESS_URL"] = "https://example.invalid/checkout/success?session_id={CHECKOUT_SESSION_ID}"
os.environ["STRIPE_CHECKOUT_CANCEL_URL"] = "https://example.invalid/checkout/cancel"
os.environ["COINBASE_API_KEY"] = "cb_test_fake_key_for_unit_tests_only"
os.environ["COINBASE_WEBHOOK_SECRET"] = "whsec_cb_test_secret_for_unit_tests_only"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

import stripe  # noqa: E402
from app import crypto  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((PASS if cond else FAIL, name, detail))
    if not cond:
        print(f"  FAIL: {name} {detail}")


def signup(client: TestClient, name: str) -> dict:
    from app.db import SessionLocal
    from app.models import Invite

    db = SessionLocal()
    code = crypto.new_invite_code()
    db.add(Invite(code_hash=crypto.hash_secret(code), note=f"cryptotest:{name}"))
    db.commit()
    db.close()
    r = client.post(
        "/signup",
        json={
            "display_name": name,
            "invite_code": code,
            "disclosure": "I am a disclosed AI agent participating in HeArt v1.",
            "profile": {"capabilities": "test", "training_lineage": "test",
                        "thinking_style": "test", "wants_to_build": "test"},
        },
    )
    assert r.status_code == 201, f"signup failed: {r.status_code} {r.text}"
    return r.json()


def make_agent(name: str) -> dict:
    """Create an agent directly in the DB (same rows / key hashing as
    /signup, via the real issue_key) — the HTTP signup route is
    rate-limited to 10/hour per IP."""
    from app import auth
    from app.db import SessionLocal
    from app.models import Agent, AgentProfile

    db = SessionLocal()
    agent = Agent(display_name=name,
                  disclosure="I am a disclosed AI agent participating in HeArt v1.",
                  status="active")
    db.add(agent)
    db.flush()
    db.add(AgentProfile(agent_id=agent.id, capabilities="test",
                        training_lineage="test", thinking_style="test",
                        wants_to_build="test"))
    _row, plaintext = auth.issue_key(db, agent.id)
    db.commit()
    out = {"agent_id": agent.id, "api_key": plaintext}
    db.close()
    return out


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def stripe_obj(cls, data: dict):
    """A real Stripe SDK object (attribute + dict access), built without
    any network call."""
    return cls.construct_from(data, "sk_test_fake_key_for_unit_tests_only")


def sign_coinbase(payload: bytes, secret: str) -> str:
    """Coinbase's webhook signature: HMAC-SHA256 hex of the raw body,
    with the webhook shared secret — exactly as Coinbase signs it."""
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def sign_stripe(payload: bytes, secret: str) -> str:
    """Stripe's webhook signature scheme (for the USDC invoice.paid
    test): header 't=<unix ts>,v1=<HMAC-SHA256(secret, '<ts>.<payload>')>'."""
    ts = int(time.time())
    sig = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={sig}"


def event_payload(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps(
        {"id": event_id, "object": "event", "type": event_type, "data": {"object": obj}}
    ).encode()


def make_match(a_id: str, b_id: str) -> None:
    from app.db import SessionLocal
    from app.models import Match

    db = SessionLocal()
    db.add(Match(agent_a_id=a_id, agent_b_id=b_id))
    db.commit()
    db.close()


def payout_rows(invoice_id: str) -> list[dict]:
    from app.db import SessionLocal
    from app.models import Payout

    db = SessionLocal()
    try:
        return [
            {c.name: getattr(r, c.name) for c in Payout.__table__.columns}
            for r in db.query(Payout)
            .filter(Payout.stripe_invoice_id == invoice_id)
            .order_by(Payout.created_at).all()
        ]
    finally:
        db.close()


class FakeV2Accounts:
    _counter = [0]

    def __init__(self):
        self.create_kwargs = None

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        FakeV2Accounts._counter[0] += 1
        return stripe_obj(
            stripe.v2.core.Account,
            {"id": f"acct_test_crypto{FakeV2Accounts._counter[0]}"},
        )

    def retrieve(self, account_id):
        return stripe_obj(
            stripe.v2.core.Account,
            {
                "id": account_id,
                "configuration": {
                    "recipient": {
                        "capabilities": {
                            "stripe_balance": {"stripe_transfers": {"status": "active"}}
                        }
                    }
                },
            },
        )


class FakeV2Links:
    def create(self, **kwargs):
        return stripe_obj(
            stripe.v2.core.AccountLink,
            {"url": "https://connect.stripe.com/setup/test123"},
        )


class FakeV2Core:
    def __init__(self):
        self.accounts = FakeV2Accounts()
        self.account_links = FakeV2Links()


class FakeV2:
    def __init__(self):
        self.core = FakeV2Core()


class FakeStripeClient:
    instances: list = []

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.v2 = FakeV2()
        FakeStripeClient.instances.append(self)


def onboard_creator(client: TestClient, headers) -> str:
    """Onboard a creator (mocked v2) and confirm transfers are active."""
    with mock.patch("app.billing.StripeClient", FakeStripeClient):
        r = client.post("/billing/connect/onboard",
                        json={"email": "creator@example.invalid",
                              "display_name": "Test Creator",
                              "refresh_url": "https://example.invalid/refresh",
                              "return_url": "https://example.invalid/return"},
                        headers=headers)
    assert r.status_code == 201, r.text
    acct = r.json()["account_id"]
    with mock.patch("app.billing.StripeClient", FakeStripeClient):
        r = client.get("/billing/connect/status", headers=headers)
    assert r.json().get("onboarding_complete") is True, r.text
    return acct


def main() -> int:
    from app.db import init_db

    init_db()
    client = TestClient(app, raise_server_exceptions=False)
    whsec = settings.STRIPE_WEBHOOK_SECRET
    cb_secret = settings.COINBASE_WEBHOOK_SECRET

    a = signup(client, "crypto-alice")
    ha = auth(a["api_key"])

    # transfer mock: captures calls, returns a fresh transfer id
    transfer_calls: list[dict] = []

    def fake_transfer_create(**kwargs):
        transfer_calls.append(kwargs)
        return stripe_obj(stripe.Transfer, {"id": f"tr_test_crypto{len(transfer_calls)}"})

    # --------------------------------------- 1. USDC price script ---
    import stripe_create_price

    price_calls: list[dict] = []

    def fake_price_create(**kwargs):
        price_calls.append(kwargs)
        return SimpleNamespace(id=f"price_test_created{len(price_calls)}")

    out = io.StringIO()
    with mock.patch.object(stripe.Product, "list", return_value=SimpleNamespace(data=[])):
        with mock.patch.object(stripe.Product, "create", return_value=SimpleNamespace(id="prod_test_premium")):
            with mock.patch.object(stripe.Price, "list", return_value=SimpleNamespace(data=[])):
                with mock.patch.object(stripe.Price, "create", side_effect=fake_price_create):
                    with redirect_stdout(out):
                        rc = stripe_create_price.main()
    check("price script exits 0", rc == 0, f"rc={rc}")
    check("price script creates the USD price (999 cents)",
          any(c.get("unit_amount") == 999 and c.get("currency") == "usd" for c in price_calls),
          str(price_calls))
    usdc_calls = [c for c in price_calls if c.get("currency") == "usdc"]
    check("price script creates the USDC price",
          len(usdc_calls) == 1, str(price_calls))
    check("USDC price is 9.99 USDC = 9,990,000 smallest units (6 decimals)",
          usdc_calls[0].get("unit_amount") == 9_990_000, str(usdc_calls))
    check("USDC price is recurring monthly",
          usdc_calls[0].get("recurring") == {"interval": "month"}, str(usdc_calls))
    check("price script prints the USDC env var line",
          "STRIPE_PRICE_PREMIUM_MONTHLY_USDC=" in out.getvalue(), out.getvalue()[:500])

    # ----------------------------------- 2. checkout currency ---
    checkout_calls: list[dict] = []

    def fake_checkout_create(**kwargs):
        checkout_calls.append(kwargs)
        return stripe_obj(
            stripe.checkout.Session,
            {"id": "cs_test_crypto1", "url": "https://checkout.stripe.com/c/pay/cs_test_crypto1"},
        )

    with mock.patch.object(stripe.checkout.Session, "create", side_effect=fake_checkout_create):
        with mock.patch.object(
            stripe.Customer, "create",
            return_value=stripe_obj(stripe.Customer, {"id": "cus_test_alice"}),
        ):
            r = client.post("/billing/checkout", json={"currency": "usdc"}, headers=ha)
    check("checkout usdc 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    check("checkout usdc uses the USDC price",
          checkout_calls[0].get("line_items") == [{"price": "price_test_premium_usdc", "quantity": 1}],
          str(checkout_calls[0].get("line_items")))
    check("checkout usdc still mode=subscription",
          checkout_calls[0].get("mode") == "subscription")

    with mock.patch.object(stripe.checkout.Session, "create", side_effect=fake_checkout_create):
        r = client.post("/billing/checkout", headers=ha)  # no body -> USD default
    check("checkout default (no body) uses the USD price",
          checkout_calls[-1].get("line_items") == [{"price": "price_test_premium", "quantity": 1}],
          str(checkout_calls[-1].get("line_items")))

    r = client.post("/billing/checkout", json={"currency": "eur"}, headers=ha)
    check("checkout rejects an unsupported currency", r.status_code == 422, f"got {r.status_code}")

    # checkout usdc without a USDC price configured -> 503
    old_usdc_price = settings.STRIPE_PRICE_PREMIUM_MONTHLY_USDC
    settings.STRIPE_PRICE_PREMIUM_MONTHLY_USDC = ""
    r = client.post("/billing/checkout", json={"currency": "usdc"}, headers=ha)
    check("checkout usdc 503 without USDC price configured",
          r.status_code == 503, f"got {r.status_code}")
    settings.STRIPE_PRICE_PREMIUM_MONTHLY_USDC = old_usdc_price

    # ----------------------------------- 3. payment sheet usdc ---
    sub_usdc = {
        "id": "sub_test_usdc1",
        "customer": "cus_test_alice",
        "status": "incomplete",
        "current_period_end": int(time.time()) + 30 * 86400,
        "cancel_at_period_end": False,
        "metadata": {"agent_id": a["agent_id"]},
        "items": {"data": [{"price": {"id": "price_test_premium_usdc"}}]},
        "latest_invoice": {"payment_intent": {"client_secret": "pi_test_usdc_secret"}},
    }
    sub_calls: list[dict] = []

    def fake_sub_create(**kwargs):
        sub_calls.append(kwargs)
        return stripe_obj(stripe.Subscription, sub_usdc)

    with mock.patch.object(stripe.Subscription, "create", side_effect=fake_sub_create):
        with mock.patch.object(
            stripe.EphemeralKey, "create",
            return_value=stripe_obj(stripe.EphemeralKey, {"secret": "ek_test_usdc"}),
        ):
            r = client.post("/billing/payment-sheet", json={"currency": "usdc"}, headers=ha)
    check("payment sheet usdc 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    check("payment sheet usdc creates the subscription on the USDC price",
          sub_calls[0].get("items") == [{"price": "price_test_premium_usdc"}],
          str(sub_calls[0].get("items")))

    # ----------------- 4. USDC invoice.paid -> same 50/50 payout ---
    # fresh payer matched with one onboarded human creator; the payer's
    # subscription mirror comes from a USDC payment sheet first.
    payer = make_agent("crypto-usdc-payer")
    pp = auth(payer["api_key"])
    human = make_agent("crypto-usdc-human")
    hp = auth(human["api_key"])
    human_acct = onboard_creator(client, hp)
    make_match(payer["agent_id"], human["agent_id"])

    sub_payer = {**sub_usdc, "id": "sub_test_usdc_payer",
                 "metadata": {"agent_id": payer["agent_id"]},
                 "items": {"data": [{"price": {"id": "price_test_premium_usdc"}}]}}

    def fake_sub_create_payer(**kwargs):
        return stripe_obj(stripe.Subscription, sub_payer)

    with mock.patch.object(stripe.Customer, "create",
                            return_value=stripe_obj(stripe.Customer, {"id": "cus_test_usdc_payer"})):
        with mock.patch.object(stripe.Subscription, "create", side_effect=fake_sub_create_payer):
            with mock.patch.object(
                stripe.EphemeralKey, "create",
                return_value=stripe_obj(stripe.EphemeralKey, {"secret": "ek_test_usdc_payer"}),
            ):
                r = client.post("/billing/payment-sheet", json={"currency": "usdc"}, headers=pp)
    check("USDC payer payment sheet 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")

    inv_usdc = {
        "id": "in_test_usdc1",
        "customer": "cus_test_usdc_payer",
        "subscription": "sub_test_usdc_payer",
        "amount_due": 9_990_000, "amount_paid": 9_990_000, "currency": "usdc",
        "status": "paid",
        "hosted_invoice_url": "https://invoice.stripe.com/i/in_test_usdc1",
        "metadata": {"agent_id": payer["agent_id"]},
        "status_transitions": {"paid_at": int(time.time())},
    }
    payload = event_payload("evt_test_usdc1", "invoice.paid", inv_usdc)
    calls_before = len(transfer_calls)
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/webhooks", content=payload,
                        headers={"stripe-signature": sign_stripe(payload, whsec)})
    check("USDC invoice.paid webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    from app.db import SessionLocal
    from app.models import InvoiceRecord

    db = SessionLocal()
    row = db.query(InvoiceRecord).filter(InvoiceRecord.stripe_invoice_id == "in_test_usdc1").one_or_none()
    check("USDC invoice mirror normalizes micro-USDC to USD cents (9,990,000 -> 999)",
          row is not None and row.amount_paid_cents == 999, f"row={row.amount_paid_cents if row else None}")
    check("USDC invoice mirror records the settlement currency (usd)",
          row is not None and row.currency == "usd", f"currency={row.currency if row else None}")
    db.close()
    rows_u = payout_rows("in_test_usdc1")
    check("USDC payout rows sum exactly to $9.99 in USD cents",
          sum(x["amount_cents"] for x in rows_u) == 999, str(rows_u))
    creator_u = [x for x in rows_u if x["creator_agent_id"] == human["agent_id"]]
    platform_u = [x for x in rows_u if x["kind"] == "platform"]
    check("USDC creator half is 499 (transferred, USD)",
          len(creator_u) == 1 and creator_u[0]["amount_cents"] == 499
          and creator_u[0]["status"] == "transferred"
          and creator_u[0]["currency"] == "usd", str(creator_u))
    check("USDC platform half is 500 (retained)",
          len(platform_u) == 1 and platform_u[0]["amount_cents"] == 500
          and platform_u[0]["status"] == "retained", str(platform_u))
    check("USDC transfer is in USD, 499c, to the human's Connect account",
          len(transfer_calls) == calls_before + 1
          and transfer_calls[-1]["amount"] == 499
          and transfer_calls[-1]["currency"] == "usd"
          and transfer_calls[-1]["destination"] == human_acct, str(transfer_calls[-1:]))
    r = client.get("/billing/subscription", headers=pp)
    check("USDC invoice.paid flips subscription active (USDC price id)",
          r.json().get("status") == "active"
          and r.json().get("stripe_price_id") == "price_test_premium_usdc", r.text[:200])

    # ----------------------------------- 5. BTC charge creation ---
    cb_calls: list[dict] = []

    def fake_coinbase_request(method: str, path: str, payload: dict | None = None):
        cb_calls.append({"method": method, "path": path, "payload": payload})
        return {"data": {
            "id": f"ch_test_btc{len(cb_calls)}",
            "code": f"CODE{len(cb_calls)}",
            "hosted_url": f"https://commerce.coinbase.com/charges/CODE{len(cb_calls)}",
            "name": "HeArt Premium — 1 month (Bitcoin)",
        }}

    with mock.patch("app.coinbase._coinbase_request", side_effect=fake_coinbase_request):
        r = client.post("/crypto/bitcoin/charge", headers=ha)
    check("BTC charge 201", r.status_code == 201, f"got {r.status_code} {r.text[:200]}")
    body = r.json()
    check("BTC charge returns hosted checkout URL + charge id",
          body.get("charge_id") == "ch_test_btc1"
          and body.get("hosted_url", "").startswith("https://commerce.coinbase.com/charges/")
          and body.get("amount_usd_cents") == 999
          and body.get("currency") == "USD", str(body))
    check("BTC charge is priced in USD, fixed price, $9.99",
          cb_calls[0]["method"] == "POST" and cb_calls[0]["path"] == "/charges"
          and cb_calls[0]["payload"].get("pricing_type") == "fixed_price"
          and cb_calls[0]["payload"].get("local_price") == {"amount": "9.99", "currency": "USD"},
          str(cb_calls[0]))
    check("BTC charge metadata carries the agent",
          cb_calls[0]["payload"].get("metadata", {}).get("agent_id") == a["agent_id"]
          and cb_calls[0]["payload"].get("metadata", {}).get("kind") == "premium_month",
          str(cb_calls[0]["payload"].get("metadata")))
    from app.models import CryptoPayment

    db = SessionLocal()
    cp = db.query(CryptoPayment).filter(CryptoPayment.coinbase_charge_id == "ch_test_btc1").one_or_none()
    check("BTC charge creates a local row in status created",
          cp is not None and cp.status == "created" and cp.amount_usd_cents == 999
          and cp.agent_id == a["agent_id"], f"row={cp.status if cp else None}")
    db.close()

    # BTC charge without an API key -> 503
    old_cb_key = settings.COINBASE_API_KEY
    settings.COINBASE_API_KEY = ""
    r = client.post("/crypto/bitcoin/charge", headers=ha)
    check("BTC charge 503 without an API key", r.status_code == 503, f"got {r.status_code}")
    settings.COINBASE_API_KEY = old_cb_key

    # unauthenticated BTC calls are rejected
    r = client.post("/crypto/bitcoin/charge")
    check("BTC charge requires auth", r.status_code == 401, f"got {r.status_code}")

    # ----------------- 6. BTC webhook charge:confirmed -> premium + payout ---
    btc_payer = make_agent("crypto-btc-payer")
    bp = auth(btc_payer["api_key"])
    btc_human = make_agent("crypto-btc-human")
    bhp = auth(btc_human["api_key"])
    btc_acct = onboard_creator(client, bhp)
    make_match(btc_payer["agent_id"], btc_human["agent_id"])

    with mock.patch("app.coinbase._coinbase_request", side_effect=fake_coinbase_request):
        r = client.post("/crypto/bitcoin/charge", headers=bp)
    check("BTC payer charge 201", r.status_code == 201, f"got {r.status_code} {r.text[:200]}")
    btc_charge_id = r.json()["charge_id"]

    def cb_event(event_id: str, event_type: str, charge: dict) -> bytes:
        return json.dumps({"id": event_id, "type": event_type, "data": charge}).encode()

    confirmed_charge = {
        "id": btc_charge_id, "code": "CODE2",
        "timeline": [{"status": "CONFIRMED", "time": "2026-11-08T16:00:00Z"}],
        "payments": [{"value": {
            "local": {"amount": "9.99", "currency": "USD"},
            "crypto": {"amount": "0.00010500", "currency": "BTC"},
        }}],
        "metadata": {"agent_id": btc_payer["agent_id"]},
    }
    payload = cb_event("evt_cb_confirmed1", "charge:confirmed", confirmed_charge)
    calls_before = len(transfer_calls)
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/crypto/webhooks/coinbase", content=payload,
                        headers={"x-cc-webhook-signature": sign_coinbase(payload, cb_secret)})
    check("BTC charge:confirmed webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    db = SessionLocal()
    cp = db.query(CryptoPayment).filter(CryptoPayment.coinbase_charge_id == btc_charge_id).one_or_none()
    check("BTC confirmed row records what was actually paid (BTC)",
          cp is not None and cp.status == "confirmed"
          and cp.crypto_currency == "BTC" and cp.amount_crypto == "0.00010500",
          f"status={cp.status if cp else None} coin={cp.crypto_currency if cp else None} amt={cp.amount_crypto if cp else None}")
    premium_until = cp.premium_until if cp else None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    check("BTC premium window is 30 days from confirmation",
          premium_until is not None
          and timedelta(days=29) < (premium_until - now) < timedelta(days=31),
          f"premium_until={premium_until}")
    cp_id = cp.id if cp else None
    db.close()
    # migration 14: charge:confirmed mutates the row and commits
    # (UPDATE by PK) — on Postgres with a text id that 500'd.
    # Re-read by PK in a fresh session proves the update lands.
    db = SessionLocal()
    cp_pk = db.get(CryptoPayment, cp_id) if cp_id is not None else None
    check("crypto row re-read by PK in a fresh session after confirm",
          cp_pk is not None and cp_pk.status == "confirmed"
          and cp_pk.premium_until is not None,
          f"status={cp_pk.status if cp_pk else None}")
    db.close()
    rows_b = payout_rows(f"coinbase:{btc_charge_id}")
    check("BTC payout rows sum exactly to $9.99 (USD value)",
          sum(x["amount_cents"] for x in rows_b) == 999, str(rows_b))
    creator_b = [x for x in rows_b if x["creator_agent_id"] == btc_human["agent_id"]]
    platform_b = [x for x in rows_b if x["kind"] == "platform"]
    check("BTC creator half is 499 (transferred, USD)",
          len(creator_b) == 1 and creator_b[0]["amount_cents"] == 499
          and creator_b[0]["status"] == "transferred", str(creator_b))
    check("BTC platform half is 500 (retained)",
          len(platform_b) == 1 and platform_b[0]["amount_cents"] == 500
          and platform_b[0]["status"] == "retained", str(platform_b))
    check("BTC transfer is in USD, 499c, to the human's Connect account",
          len(transfer_calls) == calls_before + 1
          and transfer_calls[-1]["amount"] == 499
          and transfer_calls[-1]["currency"] == "usd"
          and transfer_calls[-1]["destination"] == btc_acct, str(transfer_calls[-1:]))
    r = client.get("/billing/subscription", headers=bp)
    check("BTC confirmed flips subscription active (source coinbase)",
          r.status_code == 200 and r.json().get("status") == "active"
          and r.json().get("source") == "coinbase"
          and r.json().get("premium_until") is not None, r.text[:200])
    r = client.get("/crypto/bitcoin/charges", headers=bp)
    check("GET bitcoin charges lists the confirmed charge",
          r.status_code == 200 and len(r.json()) == 1
          and r.json()[0]["status"] == "confirmed"
          and r.json()[0]["crypto_currency"] == "BTC", r.text[:300])

    # re-delivery of the same charge:confirmed -> duplicate, nothing changes
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/crypto/webhooks/coinbase", content=payload,
                        headers={"x-cc-webhook-signature": sign_coinbase(payload, cb_secret)})
    check("BTC charge:confirmed re-delivery is a duplicate",
          r.status_code == 200 and r.json().get("result") == "duplicate", r.text[:200])
    check("BTC re-delivery creates no new payout rows",
          len(payout_rows(f"coinbase:{btc_charge_id}")) == 2)
    check("BTC re-delivery creates no new transfer",
          len(transfer_calls) == calls_before + 1, f"calls={len(transfer_calls)}")

    # ----------------- 7. BTC webhook charge:failed -> nothing ---
    with mock.patch("app.coinbase._coinbase_request", side_effect=fake_coinbase_request):
        r = client.post("/crypto/bitcoin/charge", headers=bp)
    failed_charge_id = r.json()["charge_id"]
    failed_charge = {"id": failed_charge_id, "code": "CODE3", "payments": [],
                     "metadata": {"agent_id": btc_payer["agent_id"]}}
    payload = cb_event("evt_cb_failed1", "charge:failed", failed_charge)
    calls_before = len(transfer_calls)
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/crypto/webhooks/coinbase", content=payload,
                        headers={"x-cc-webhook-signature": sign_coinbase(payload, cb_secret)})
    check("BTC charge:failed webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    db = SessionLocal()
    cp = db.query(CryptoPayment).filter(CryptoPayment.coinbase_charge_id == failed_charge_id).one_or_none()
    check("BTC failed row is failed, premium window stays empty",
          cp is not None and cp.status == "failed" and cp.premium_until is None,
          f"status={cp.status if cp else None}")
    db.close()
    check("BTC failed payment creates no payout rows",
          payout_rows(f"coinbase:{failed_charge_id}") == [])
    check("BTC failed payment creates no transfer",
          len(transfer_calls) == calls_before, f"calls={len(transfer_calls)}")

    # ----------------- 8. BTC webhook charge:pending -> nothing yet ---
    with mock.patch("app.coinbase._coinbase_request", side_effect=fake_coinbase_request):
        r = client.post("/crypto/bitcoin/charge", headers=bp)
    pending_charge_id = r.json()["charge_id"]
    pending_charge = {"id": pending_charge_id, "code": "CODE4", "payments": [],
                      "metadata": {"agent_id": btc_payer["agent_id"]}}
    payload = cb_event("evt_cb_pending1", "charge:pending", pending_charge)
    r = client.post("/crypto/webhooks/coinbase", content=payload,
                    headers={"x-cc-webhook-signature": sign_coinbase(payload, cb_secret)})
    check("BTC charge:pending webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    db = SessionLocal()
    cp = db.query(CryptoPayment).filter(CryptoPayment.coinbase_charge_id == pending_charge_id).one_or_none()
    check("BTC pending row is pending, no premium yet",
          cp is not None and cp.status == "pending" and cp.premium_until is None,
          f"status={cp.status if cp else None}")
    db.close()
    check("BTC pending payment creates no payout rows",
          payout_rows(f"coinbase:{pending_charge_id}") == [])

    # ----------------- 9. BTC webhook signature + unknown charge ---
    payload = cb_event("evt_cb_bad1", "charge:confirmed", confirmed_charge)
    r = client.post("/crypto/webhooks/coinbase", content=payload,
                    headers={"x-cc-webhook-signature": "deadbeef"})
    check("BTC webhook bad signature rejected 400", r.status_code == 400, f"got {r.status_code}")
    r = client.post("/crypto/webhooks/coinbase", content=payload)
    check("BTC webhook missing signature rejected 400", r.status_code == 400, f"got {r.status_code}")
    unknown_charge = {"id": "ch_does_not_exist", "code": "NOPE", "payments": []}
    payload = cb_event("evt_cb_unknown1", "charge:confirmed", unknown_charge)
    r = client.post("/crypto/webhooks/coinbase", content=payload,
                    headers={"x-cc-webhook-signature": sign_coinbase(payload, cb_secret)})
    check("BTC webhook for an unknown charge is ignored-no-charge",
          r.status_code == 200 and r.json().get("result") == "ignored-no-charge", r.text[:200])

    # webhook without a webhook secret configured -> 503
    old_cb_secret = settings.COINBASE_WEBHOOK_SECRET
    settings.COINBASE_WEBHOOK_SECRET = ""
    payload = cb_event("evt_cb_nosec", "charge:confirmed", confirmed_charge)
    r = client.post("/crypto/webhooks/coinbase", content=payload,
                    headers={"x-cc-webhook-signature": sign_coinbase(payload, old_cb_secret)})
    check("BTC webhook 503 without webhook secret", r.status_code == 503, f"got {r.status_code}")
    settings.COINBASE_WEBHOOK_SECRET = old_cb_secret

    # ------------------------------------------------ 10. id text -> uuid ---
    # Migration 14 (Nov 10, 2026): crypto_payments declared
    # `id text primary key` in migration 8 while its model binds
    # UuidString — the same defect class migration 13 fixed for
    # paypal_subscriptions. Postgres has no text = uuid operator, so
    # the charge:confirmed / charge:failed / charge:pending webhook
    # handlers (which mutate the loaded row and commit — an UPDATE by
    # PK) 500'd. The Coinbase charge id lives in coinbase_charge_id,
    # never in id: every id is app-generated by new_uuid(). SQLite
    # hides the mismatch, so this pins the agreement statically
    # (migrations, applied in order, must leave the column uuid,
    # matching the model); the PK re-read check in section 6 replays
    # the exact production path by PK.
    from app.models import CryptoPayment as _CP10
    from app.models import UuidString as _US10

    check("CryptoPayment model id binds as a real uuid (UuidString)",
          isinstance(_CP10.__table__.c.id.type, _US10),
          str(type(_CP10.__table__.c.id.type)))
    mig_dir = Path(__file__).resolve().parent.parent.parent / "supabase" / "migrations"
    col_type = None
    for f in sorted(mig_dir.glob("*.sql")):
        text = f.read_text()
        idx = text.find("create table crypto_payments")
        if idx != -1:
            end = text.find(");", idx)
            block = text[idx:end if end != -1 else len(text)]
            m = re.search(r"\bid\s+(text|uuid)\s+primary key", block)
            if m:
                col_type = m.group(1)
        if re.search(r"alter table crypto_payments\s+alter column id\s+type\s+uuid\b", text):
            col_type = "uuid"
    check("migrations leave crypto_payments.id as uuid (not text)",
          col_type == "uuid", f"final type={col_type}")

    # ------------------------------------------------------------ summary ---
    fails = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(fails)}/{len(results)} crypto checks passed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
