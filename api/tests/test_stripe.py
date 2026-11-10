# HeArt v1 API — invented by John Davis.
"""End-to-end verification of the Stripe billing integration (HeArt Premium;
plan: stripe-integration-plan-2026-11-07), run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_stripe.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  billing disabled without a key (503) / live-key guard (test mode only) ->
  hosted Checkout session (mode=subscription, flat-rate price, success/cancel
  URLs, agent metadata) ->
  Mobile Payment Sheet (customer reuse, ephemeral key, incomplete
  subscription + PaymentIntent client secret) ->
  Customer Portal session ->
  subscription status (free tier -> active after webhook) ->
  invoice creation on a business event (auto-advance + charge automatically,
  finalize + pay) ->
  webhooks: REAL signature verification (payloads signed with HMAC-SHA256
  exactly as Stripe signs them — bad/missing signature is rejected 400),
  invoice.paid / invoice.payment_failed / charge.refunded /
  credit_note.created / checkout.session.completed dispatch, and
  idempotency (a re-delivered event is processed once) ->
  Connect v2 (recipient config + stripe_transfers, onboarding link, status)
  -> creator payouts (John's calls, Nov 8): a subscription payment
  triggers a 50/50 payout — human creators by Stripe Transfer, AI
  creators HELD in the queryable ledger (never transferred) ->
  tax status (threshold monitoring only, no collection).

Stripe API calls are mocked at the SDK boundary (no test key exists in
this environment and the sandbox egress proxy breaks TLS to Stripe, so a
live test-mode smoke test is NOT possible here — see
docs/stripe-integration.md for the commands to run it once a test key
exists). What IS verified for real: the full HTTP stack, the exact SDK
call arguments, the local mirrors, and webhook signature crypto.

KEY RULES UNDER TEST: test mode only (a live key is refused); webhooks
verify the Stripe signature before touching state; no card data ever
touches HeArt; creator payouts are 50/50 on a subscription payment —
human creators by Stripe Transfer, AI creators HELD in the ledger,
never transferred; payout rows for one invoice always sum exactly to
the amount paid; re-delivery changes nothing.
"""

import hashlib
import hmac
import io
import json
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_stripe_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py
os.environ["MAX_WORKS_PER_DAY"] = "10"  # test process only; production default stays 1/day
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake_key_for_unit_tests_only"
os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_fake_key_for_unit_tests_only"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret_for_unit_tests_only"
os.environ["STRIPE_PRICE_PREMIUM_MONTHLY"] = "price_test_premium"
os.environ["STRIPE_CHECKOUT_SUCCESS_URL"] = "https://example.invalid/checkout/success?session_id={CHECKOUT_SESSION_ID}"
os.environ["STRIPE_CHECKOUT_CANCEL_URL"] = "https://example.invalid/checkout/cancel"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
    db.add(Invite(code_hash=crypto.hash_secret(code), note=f"stripetest:{name}"))
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
    rate-limited to 10/hour per IP, and the payout tests need more
    agents than that on top of alice + bob. Returns the same shape as
    signup() ({agent_id, api_key})."""
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


def sign_payload(payload: bytes, secret: str) -> str:
    """Stripe's webhook signature scheme, exactly as Stripe signs it:
    header 't=<unix ts>,v1=<HMAC-SHA256(secret, '<ts>.<payload>')>'."""
    ts = int(time.time())
    sig = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={sig}"


def event_payload(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps(
        {"id": event_id, "object": "event", "type": event_type, "data": {"object": obj}}
    ).encode()


def stripe_obj(cls, data: dict):
    """A real Stripe SDK object (attribute + dict access), built without
    any network call."""
    return cls.construct_from(data, "sk_test_fake_key_for_unit_tests_only")


def main() -> int:
    from app.db import init_db

    init_db()  # create tables in the isolated test DB (the lifespan would
    # also do this under `with TestClient(app)`, but this test drives the
    # client directly so it creates them explicitly)
    client = TestClient(app, raise_server_exceptions=False)
    whsec = settings.STRIPE_WEBHOOK_SECRET

    # ---------------------------------------------------------- 1. setup ---
    a = signup(client, "stripe-alice")
    ha = auth(a["api_key"])
    agent_id = a["agent_id"]

    # billing is enabled with the fake test key...
    from app import billing

    check("billing enabled with test key", billing.billing_enabled())

    # ...and the live-key guard refuses a live key while test-only is on.
    old_key, old_flag = settings.STRIPE_SECRET_KEY, settings.STRIPE_TEST_MODE_ONLY
    settings.STRIPE_SECRET_KEY = "sk_live_would_be_a_real_key"
    settings.STRIPE_TEST_MODE_ONLY = True
    try:
        billing._require_test_mode_key()
        check("live key refused in test-only mode", False, "no exception raised")
    except RuntimeError:
        check("live key refused in test-only mode", True)
    settings.STRIPE_SECRET_KEY = "sk_test_fake_key_for_unit_tests_only"
    settings.STRIPE_TEST_MODE_ONLY = True

    # billing disabled entirely without a key -> 503, app still runs.
    settings.STRIPE_SECRET_KEY = ""
    r = client.post("/billing/checkout", headers=ha)
    check("checkout 503 without a key", r.status_code == 503, f"got {r.status_code}")
    check("billing_enabled False without key", not billing.billing_enabled())
    settings.STRIPE_SECRET_KEY = old_key

    # ------------------------------------------------ 2. hosted checkout ---
    checkout_calls: list[dict] = []

    def fake_checkout_create(**kwargs):
        checkout_calls.append(kwargs)
        return stripe_obj(
            stripe.checkout.Session,
            {"id": "cs_test_checkout123", "url": "https://checkout.stripe.com/c/pay/cs_test_checkout123",
             # Stripe filtered "paypal" out: native PayPal is Europe/UK-only
             # and this is a US account — the session reports what it offered.
             "payment_method_types": ["card", "cashapp"]},
        )

    with mock.patch.object(stripe.checkout.Session, "create", side_effect=fake_checkout_create):
        with mock.patch.object(
            stripe.Customer, "create",
            return_value=stripe_obj(stripe.Customer, {"id": "cus_test_alice"}),
        ):
            r = client.post("/billing/checkout", headers=ha)
    check("checkout 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    body = r.json()
    check("checkout returns url + session id",
          body.get("checkout_url", "").startswith("https://checkout.stripe.com")
          and body.get("session_id") == "cs_test_checkout123", str(body))
    check("checkout passes allowed_payment_method_types card+cashapp+paypal",
          checkout_calls[0].get("allowed_payment_method_types") == ["card", "cashapp", "paypal"],
          str(checkout_calls[0].get("allowed_payment_method_types")))
    check("checkout does not pass removed payment_method_types (400 on API 2026-09-30+)",
          "payment_method_types" not in checkout_calls[0])
    check("checkout returns the payment methods Stripe actually offered",
          body.get("payment_method_types") == ["card", "cashapp"], str(body))
    check("checkout mode=subscription", checkout_calls[0].get("mode") == "subscription")
    check("checkout flat-rate price",
          checkout_calls[0].get("line_items") == [{"price": "price_test_premium", "quantity": 1}],
          str(checkout_calls[0].get("line_items")))
    check("checkout success/cancel urls set",
          checkout_calls[0].get("success_url") == settings.STRIPE_CHECKOUT_SUCCESS_URL
          and checkout_calls[0].get("cancel_url") == settings.STRIPE_CHECKOUT_CANCEL_URL)
    check("checkout metadata carries agent_id",
          checkout_calls[0].get("metadata", {}).get("agent_id") == agent_id)
    check("checkout subscription metadata carries agent_id",
          checkout_calls[0].get("subscription_data", {}).get("metadata", {}).get("agent_id") == agent_id)
    check("checkout no automatic_tax while tax collection off",
          "automatic_tax" not in checkout_calls[0])
    check("checkout no customer_update while tax collection off",
          "customer_update" not in checkout_calls[0])

    # tax on -> automatic_tax is sent (collection starts before first live charge)
    settings.STRIPE_TAX_ENABLED = True
    with mock.patch.object(stripe.checkout.Session, "create", side_effect=fake_checkout_create):
        r = client.post("/billing/checkout", headers=ha)
    check("checkout sends automatic_tax when tax enabled",
          checkout_calls[-1].get("automatic_tax") == {"enabled": True})
    check("checkout collects address for automatic tax (address-less customers 502 without it)",
          checkout_calls[-1].get("customer_update") == {"address": "auto"})
    settings.STRIPE_TAX_ENABLED = False

    # checkout without a price configured -> 503
    old_price = settings.STRIPE_PRICE_PREMIUM_MONTHLY
    settings.STRIPE_PRICE_PREMIUM_MONTHLY = ""
    r = client.post("/billing/checkout", headers=ha)
    check("checkout 503 without price configured", r.status_code == 503, f"got {r.status_code}")
    settings.STRIPE_PRICE_PREMIUM_MONTHLY = old_price

    # session without payment_method_types (older shape) -> [] not a crash
    def fake_checkout_no_types(**kwargs):
        return stripe_obj(
            stripe.checkout.Session,
            {"id": "cs_test_notypes", "url": "https://checkout.stripe.com/c/pay/cs_test_notypes"},
        )

    with mock.patch.object(stripe.checkout.Session, "create", side_effect=fake_checkout_no_types):
        r = client.post("/billing/checkout", headers=ha)
    check("checkout payment_method_types defaults to [] when session omits it",
          r.status_code == 200 and r.json().get("payment_method_types") == [],
          f"got {r.status_code} {r.text[:200]}")

    # ---------------------------------------------- 3. payment sheet ---
    sub_data = {
        "id": "sub_test_sheet1",
        "customer": "cus_test_alice",
        "status": "incomplete",
        "current_period_end": int(time.time()) + 30 * 86400,
        "cancel_at_period_end": False,
        "metadata": {"agent_id": agent_id},
        "items": {"data": [{"price": {"id": "price_test_premium"}}]},
        "latest_invoice": {"payment_intent": {"client_secret": "pi_test_secret_abc"}},
    }
    sub_calls: list[dict] = []

    def fake_sub_create(**kwargs):
        sub_calls.append(kwargs)
        return stripe_obj(stripe.Subscription, sub_data)

    with mock.patch.object(stripe.Customer, "create", return_value=stripe_obj(stripe.Customer, {"id": "cus_test_alice"})):
        with mock.patch.object(stripe.Subscription, "create", side_effect=fake_sub_create):
            with mock.patch.object(
                stripe.EphemeralKey, "create",
                return_value=stripe_obj(stripe.EphemeralKey, {"secret": "ek_test_ephemeral"}),
            ):
                r = client.post("/billing/payment-sheet", headers=ha)
    check("payment sheet 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    body = r.json()
    check("payment sheet returns all four fields",
          body.get("publishable_key") == "pk_test_fake_key_for_unit_tests_only"
          and body.get("customer_id") == "cus_test_alice"
          and body.get("ephemeral_key_secret") == "ek_test_ephemeral"
          and body.get("payment_intent_client_secret") == "pi_test_secret_abc"
          and body.get("subscription_id") == "sub_test_sheet1", str(body))
    check("payment sheet subscription is default_incomplete",
          sub_calls[0].get("payment_behavior") == "default_incomplete")
    check("payment sheet saves payment method on subscription",
          sub_calls[0].get("payment_settings", {}).get("save_default_payment_method") == "on_subscription")
    check("payment sheet expands payment intent",
          sub_calls[0].get("expand") == ["latest_invoice.payment_intent"])

    # second payment-sheet call reuses the same customer (no new customer)
    with mock.patch.object(stripe.Customer, "create", side_effect=AssertionError("customer recreated")):
        with mock.patch.object(stripe.Subscription, "create", side_effect=fake_sub_create):
            with mock.patch.object(
                stripe.EphemeralKey, "create",
                return_value=stripe_obj(stripe.EphemeralKey, {"secret": "ek_test_ephemeral2"}),
            ):
                r = client.post("/billing/payment-sheet", headers=ha)
    check("payment sheet reuses existing customer", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")

    # subscription status now mirrors the incomplete subscription
    r = client.get("/billing/subscription", headers=ha)
    check("subscription status mirrors incomplete sub",
          r.status_code == 200 and r.json().get("status") == "incomplete"
          and r.json().get("stripe_price_id") == "price_test_premium", r.text[:200])

    # ------------------------------------------------ 4. portal + tax ---
    with mock.patch.object(
        stripe.billing_portal.Session, "create",
        return_value=stripe_obj(stripe.billing_portal.Session, {"url": "https://billing.stripe.com/p/session/test123"}),
    ):
        r = client.post("/billing/portal", json={"return_url": "https://example.invalid/account"}, headers=ha)
    check("portal 200 + url", r.status_code == 200 and r.json().get("portal_url", "").startswith("https://billing.stripe.com"), r.text[:200])

    r = client.get("/billing/tax/status", headers=ha)
    check("tax status monitoring-only, no collection",
          r.status_code == 200 and r.json().get("collection_enabled") is False
          and r.json().get("monitoring") == "threshold-monitoring", r.text[:200])

    # ------------------------------------------------ 5. invoices ---
    inv_calls: dict = {}

    def fake_item_create(**kwargs):
        inv_calls["item"] = kwargs
        return stripe_obj(stripe.InvoiceItem, {"id": "ii_test_1"})

    inv_final = {
        "id": "in_test_manual1",
        "customer": "cus_test_alice",
        "subscription": "sub_test_sheet1",
        "amount_due": 999,
        "amount_paid": 999,
        "currency": "usd",
        "status": "paid",
        "hosted_invoice_url": "https://invoice.stripe.com/i/in_test_manual1",
        "metadata": {"agent_id": agent_id},
        "status_transitions": {"paid_at": int(time.time())},
    }

    def fake_inv_create(**kwargs):
        inv_calls["invoice"] = kwargs
        return stripe_obj(stripe.Invoice, {**inv_final, "status": "draft", "amount_paid": 0})

    with mock.patch.object(stripe.InvoiceItem, "create", side_effect=fake_item_create):
        with mock.patch.object(stripe.Invoice, "create", side_effect=fake_inv_create):
            with mock.patch.object(stripe.Invoice, "finalize_invoice", return_value=stripe_obj(stripe.Invoice, {**inv_final, "status": "open", "amount_paid": 0})):
                with mock.patch.object(stripe.Invoice, "pay", return_value=stripe_obj(stripe.Invoice, inv_final)):
                    r = client.post("/billing/invoices",
                                    json={"amount_cents": 999, "currency": "usd", "description": "manual charge"},
                                    headers=ha)
    check("invoice create 201", r.status_code == 201, f"got {r.status_code} {r.text[:200]}")
    body = r.json()
    check("invoice returns paid invoice",
          body.get("invoice_id") == "in_test_manual1" and body.get("status") == "paid"
          and body.get("amount_paid_cents") == 999, str(body))
    check("invoice item carries amount + description",
          inv_calls["item"].get("amount") == 999 and inv_calls["item"].get("description") == "manual charge")
    check("invoice auto-advance + charge automatically",
          inv_calls["invoice"].get("auto_advance") is True
          and inv_calls["invoice"].get("collection_method") == "charge_automatically",
          str(inv_calls["invoice"]))
    check("invoice amount <= 0 rejected", client.post(
        "/billing/invoices", json={"amount_cents": 0, "description": "x"}, headers=ha
    ).status_code in (400, 422))

    # ------------------------------------------------ 6. webhooks ---
    # bad signature -> 400, nothing processed
    payload = event_payload("evt_test_bad1", "invoice.paid", {**inv_final, "id": "in_test_bad"})
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": "t=123,v1=deadbeef"})
    check("webhook bad signature rejected 400", r.status_code == 400, f"got {r.status_code}")

    # missing signature header -> 400
    r = client.post("/billing/webhooks", content=payload)
    check("webhook missing signature rejected 400", r.status_code == 400, f"got {r.status_code}")

    # REAL signed invoice.paid -> 200, mirror created, subscription active
    inv_paid = {
        "id": "in_test_paid1",
        "customer": "cus_test_alice",
        "subscription": "sub_test_sheet1",
        "amount_due": 999,
        "amount_paid": 999,
        "currency": "usd",
        "status": "paid",
        "hosted_invoice_url": "https://invoice.stripe.com/i/in_test_paid1",
        "metadata": {"agent_id": agent_id},
        "status_transitions": {"paid_at": int(time.time())},
    }
    payload = event_payload("evt_test_paid1", "invoice.paid", inv_paid)
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook invoice.paid 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    check("webhook invoice.paid result processed", r.json().get("result") == "processed", r.text[:200])
    r = client.get("/billing/subscription", headers=ha)
    check("invoice.paid flips subscription active",
          r.json().get("status") == "active", r.text[:200])

    # re-delivery of the same event -> duplicate, no double-processing
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook re-delivery is a duplicate",
          r.status_code == 200 and r.json().get("result") == "duplicate", r.text[:200])
    from app.db import SessionLocal
    from app.models import InvoiceRecord

    db = SessionLocal()
    n = db.query(InvoiceRecord).filter(InvoiceRecord.stripe_invoice_id == "in_test_paid1").count()
    db.close()
    check("invoice.paid processed exactly once", n == 1, f"count={n}")

    # A Checkout subscription's first invoice can arrive with subscription
    # null (seen live Nov 9, 2026 — the mirror and the payout rows copied
    # from it were left unlinked). The mirror must fall back to the
    # agent's active subscription so the invoice stays linked.
    inv_nosub = {
        "id": "in_test_nosub1",
        "customer": "cus_test_alice",
        "subscription": None,
        "amount_due": 999,
        "amount_paid": 999,
        "currency": "usd",
        "status": "paid",
        "hosted_invoice_url": "https://invoice.stripe.com/i/in_test_nosub1",
        "metadata": {"agent_id": agent_id},
        "status_transitions": {"paid_at": int(time.time())},
    }
    payload = event_payload("evt_test_nosub1", "invoice.paid", inv_nosub)
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook invoice.paid with null subscription 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    check("webhook invoice.paid with null subscription processed",
          r.json().get("result") == "processed", r.text[:200])
    db = SessionLocal()
    row = db.query(InvoiceRecord).filter(InvoiceRecord.stripe_invoice_id == "in_test_nosub1").one_or_none()
    db.close()
    check("invoice with null subscription links to agent's active subscription",
          row is not None and row.stripe_subscription_id == "sub_test_sheet1",
          f"got {row.stripe_subscription_id if row else None}")

    # invoice.payment_failed -> subscription past_due
    inv_failed = {**inv_paid, "id": "in_test_failed1", "status": "open", "amount_paid": 0}
    payload = event_payload("evt_test_failed1", "invoice.payment_failed", inv_failed)
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook invoice.payment_failed processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    r = client.get("/billing/subscription", headers=ha)
    check("invoice.payment_failed marks subscription past_due",
          r.json().get("status") == "past_due", r.text[:200])

    # subscription.updated -> mirror follows
    sub_updated = {**sub_data, "status": "active"}
    payload = event_payload("evt_test_subupd1", "customer.subscription.updated", sub_updated)
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook subscription.updated processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    r = client.get("/billing/subscription", headers=ha)
    check("subscription.updated mirror follows", r.json().get("status") == "active", r.text[:200])

    # charge.refunded -> refund reconciled against its invoice
    charge = {"id": "ch_test_ref1", "invoice": "in_test_paid1", "amount_refunded": 999}
    payload = event_payload("evt_test_ref1", "charge.refunded", charge)
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook charge.refunded processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    db = SessionLocal()
    row = db.query(InvoiceRecord).filter(InvoiceRecord.stripe_invoice_id == "in_test_paid1").one()
    check("charge.refunded reconciled: refunded cents + status refunded",
          row.amount_refunded_cents == 999 and row.status == "refunded",
          f"refunded={row.amount_refunded_cents} status={row.status}")
    db.close()

    # credit_note.created -> credit reconciled
    note = {"id": "cn_test_1", "invoice": "in_test_paid1", "amount": 250}
    payload = event_payload("evt_test_cn1", "credit_note.created", note)
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook credit_note.created processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    db = SessionLocal()
    row = db.query(InvoiceRecord).filter(InvoiceRecord.stripe_invoice_id == "in_test_paid1").one()
    check("credit_note.created reconciled: credit cents",
          row.credit_total_cents == 250, f"credit={row.credit_total_cents}")
    db.close()

    # checkout.session.completed -> customer linked to agent
    b = signup(client, "stripe-bob")
    hb = auth(b["api_key"])
    sess = {"id": "cs_test_bob1", "customer": "cus_test_bob",
            "subscription": "sub_test_bob1", "metadata": {"agent_id": b["agent_id"]}}
    payload = event_payload("evt_test_co1", "checkout.session.completed", sess)
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook checkout.session.completed processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    from app.models import BillingCustomer

    db = SessionLocal()
    row = db.get(BillingCustomer, b["agent_id"])
    check("checkout.session.completed links customer to agent",
          row is not None and row.stripe_customer_id == "cus_test_bob")
    db.close()

    # unknown event type -> ignored, still recorded (idempotent)
    payload = event_payload("evt_test_unknown1", "payment_intent.succeeded", {"id": "pi_x"})
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook unknown type ignored",
          r.status_code == 200 and r.json().get("result") == "ignored", r.text[:200])
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, whsec)})
    check("webhook unknown type re-delivery is duplicate",
          r.json().get("result") == "duplicate", r.text[:200])

    # webhook without a webhook secret configured -> 503
    old_whsec = settings.STRIPE_WEBHOOK_SECRET
    settings.STRIPE_WEBHOOK_SECRET = ""
    payload = event_payload("evt_test_nosec", "invoice.paid", inv_paid)
    r = client.post("/billing/webhooks", content=payload,
                    headers={"stripe-signature": sign_payload(payload, old_whsec)})
    check("webhook 503 without webhook secret", r.status_code == 503, f"got {r.status_code}")
    settings.STRIPE_WEBHOOK_SECRET = old_whsec

    # ------------------------------------------------ 7. connect v2 ---
    mock_link = stripe_obj(
        stripe.v2.core.AccountLink,
        {"url": "https://connect.stripe.com/setup/test123"},
    )

    class FakeV2Accounts:
        _counter = [0]

        def __init__(self):
            self.create_kwargs = None

        def create(self, **kwargs):
            self.create_kwargs = kwargs
            # Distinct account id per onboard (connect_accounts.stripe_account_id
            # is unique) — first create is still acct_test_creator1.
            FakeV2Accounts._counter[0] += 1
            return stripe_obj(
                stripe.v2.core.Account,
                {"id": f"acct_test_creator{FakeV2Accounts._counter[0]}"},
            )

        def retrieve(self, account_id):
            # Echo the requested account, transfers active (the capability
            # check get_connect_status reads).
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
        def __init__(self):
            self.create_kwargs = None

        def create(self, **kwargs):
            self.create_kwargs = kwargs
            return mock_link

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

    with mock.patch("app.billing.StripeClient", FakeStripeClient):
        r = client.post("/billing/connect/onboard",
                        json={"email": "creator@example.invalid", "display_name": "Test Creator",
                              "refresh_url": "https://example.invalid/refresh",
                              "return_url": "https://example.invalid/return"},
                        headers=ha)
    check("connect onboard 201", r.status_code == 201, f"got {r.status_code} {r.text[:200]}")
    body = r.json()
    check("connect onboard returns account id",
          body.get("account_id") == "acct_test_creator1" and body.get("already_exists") is False, str(body))
    create_kwargs = FakeStripeClient.instances[0].v2.core.accounts.create_kwargs
    check("connect v2 recipient config requested",
          create_kwargs.get("configuration") == {
              "recipient": {"capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}}
          }, str(create_kwargs.get("configuration")))
    check("connect onboard used test key",
          FakeStripeClient.instances[0].api_key == "sk_test_fake_key_for_unit_tests_only")

    # repeat onboard -> existing account, no new Stripe call
    with mock.patch("app.billing.StripeClient", FakeStripeClient):
        before = len(FakeStripeClient.instances)
        r = client.post("/billing/connect/onboard",
                        json={"email": "creator@example.invalid", "display_name": "Test Creator",
                              "refresh_url": "https://example.invalid/refresh",
                              "return_url": "https://example.invalid/return"},
                        headers=ha)
    check("connect onboard repeat returns existing",
          r.status_code == 201 and r.json().get("already_exists") is True
          and len(FakeStripeClient.instances) == before, r.text[:200])

    with mock.patch("app.billing.StripeClient", FakeStripeClient):
        r = client.post("/billing/connect/link",
                        json={"refresh_url": "https://example.invalid/refresh",
                              "return_url": "https://example.invalid/return"},
                        headers=ha)
    check("connect onboarding link 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    check("connect onboarding link url returned",
          r.json().get("onboarding_url") == "https://connect.stripe.com/setup/test123", r.text[:200])
    link_kwargs = FakeStripeClient.instances[-1].v2.core.account_links.create_kwargs
    check("connect link is account_onboarding use case",
          link_kwargs.get("use_case", {}).get("type") == "account_onboarding"
          and link_kwargs.get("account") == "acct_test_creator1", str(link_kwargs))

    with mock.patch("app.billing.StripeClient", FakeStripeClient):
        r = client.get("/billing/connect/status", headers=ha)
    check("connect status reports transfers active",
          r.status_code == 200 and r.json().get("connected") is True
          and r.json().get("transfers_status") == "active"
          and r.json().get("onboarding_complete") is True, r.text[:200])

    # connect status for an agent with no account -> connected False
    r = client.get("/billing/connect/status", headers=hb)
    check("connect status false when no account",
          r.status_code == 200 and r.json().get("connected") is False, r.text[:200])

    # ----------------------------------------- 8. creator payouts ---
    # John's calls (Nov 8): $9.99/month; a subscription payment triggers
    # a payout; split HALF (50/50); AI shares are HELD in the ledger
    # (no bank transfer is ever attempted for an AI creator).

    def give_identity_serial(agent_id: str, serial: str) -> None:
        from app.db import SessionLocal
        from app.models import AgentIdentity

        db = SessionLocal()
        db.add(AgentIdentity(
            agent_id=agent_id, serial=serial,
            created_at="2026-11-08T00:00:00Z", maker="test-maker",
            model_lineage="test-model", capabilities='["test"]',
            public_key="ab" * 32, creator_public_key="cd" * 32,
            creator_signature="ef" * 64,
        ))
        db.commit()
        db.close()

    def make_match(a_id: str, b_id: str) -> None:
        from app.db import SessionLocal
        from app.models import Match

        db = SessionLocal()
        db.add(Match(agent_a_id=a_id, agent_b_id=b_id))
        db.commit()
        db.close()

    def onboard_creator(headers) -> str:
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

    def paid_invoice(invoice_id: str, payer_agent_id: str, subscription_id: str) -> dict:
        return {
            "id": invoice_id, "customer": "cus_test_payout",
            "subscription": subscription_id, "status": "paid",
            "amount_due": 999, "amount_paid": 999, "currency": "usd",
            "hosted_invoice_url": f"https://pay.stripe.com/invoice/{invoice_id}",
            "status_transitions": {"paid_at": int(time.time())},
            "metadata": {"agent_id": payer_agent_id},
        }

    # transfer mock: captures calls, returns a fresh transfer id; can be
    # flipped to fail to prove the failure path
    transfer_calls: list[dict] = []
    transfer_fail = {"on": False}

    def fake_transfer_create(**kwargs):
        transfer_calls.append(kwargs)
        if transfer_fail["on"]:
            raise stripe.error.StripeError("transfer declined in test")
        return stripe_obj(stripe.Transfer, {"id": f"tr_test_payout{len(transfer_calls)}"})

    # --- A. webhook invoice.paid triggers 50/50 payouts ---
    # payer matched with one human creator and one AI creator
    payer = make_agent("stripe-payer")
    pp = auth(payer["api_key"])
    human = make_agent("stripe-human-creator")
    hp = auth(human["api_key"])
    ai = make_agent("stripe-ai-creator")
    ap = auth(ai["api_key"])
    give_identity_serial(ai["agent_id"], "HEART-AI-000001")
    human_acct = onboard_creator(hp)
    make_match(payer["agent_id"], human["agent_id"])
    make_match(payer["agent_id"], ai["agent_id"])

    payload_a = event_payload("evt_test_payout1", "invoice.paid",
                              paid_invoice("in_test_payout1", payer["agent_id"], "sub_test_payout1"))
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/webhooks", content=payload_a,
                        headers={"stripe-signature": sign_payload(payload_a, whsec)})
    check("payout webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    rows_a = payout_rows("in_test_payout1")
    check("payout rows created (3: platform + 2 creators)",
          len(rows_a) == 3, f"got {len(rows_a)}")
    platform_a = [x for x in rows_a if x["kind"] == "platform"]
    human_a = [x for x in rows_a if x["creator_agent_id"] == human["agent_id"]]
    ai_a = [x for x in rows_a if x["creator_agent_id"] == ai["agent_id"]]
    check("payout rows sum exactly to the payment",
          sum(x["amount_cents"] for x in rows_a) == 999,
          str([x["amount_cents"] for x in rows_a]))
    check("platform retains 501 (500 + the 1 leftover cent)",
          len(platform_a) == 1 and platform_a[0]["amount_cents"] == 501
          and platform_a[0]["status"] == "retained", str(platform_a))
    check("human creator 249 transferred",
          len(human_a) == 1 and human_a[0]["amount_cents"] == 249
          and human_a[0]["status"] == "transferred" and human_a[0]["creator_kind"] == "human"
          and human_a[0]["stripe_transfer_id"] is not None, str(human_a))
    check("AI creator 249 held, never transferred, serial on the row",
          len(ai_a) == 1 and ai_a[0]["amount_cents"] == 249
          and ai_a[0]["status"] == "held" and ai_a[0]["creator_kind"] == "ai"
          and ai_a[0]["stripe_transfer_id"] is None
          and ai_a[0]["creator_serial"] == "HEART-AI-000001", str(ai_a))
    check("transfer called once, to the human's Connect account, 249c",
          len(transfer_calls) == 1 and transfer_calls[0]["amount"] == 249
          and transfer_calls[0]["destination"] == human_acct
          and transfer_calls[0]["currency"] == "usd", str(transfer_calls))

    # the held AI share is queryable in the ledger (GET /billing/connect/payouts)
    r = client.get("/billing/connect/payouts", headers=ap)
    ledger_ai = r.json()
    check("GET payouts: AI creator sees their held share",
          r.status_code == 200 and len(ledger_ai) == 1
          and ledger_ai[0]["status"] == "held" and ledger_ai[0]["amount_cents"] == 249
          and ledger_ai[0]["creator_serial"] == "HEART-AI-000001"
          and ledger_ai[0]["stripe_invoice_id"] == "in_test_payout1", r.text[:300])
    r = client.get("/billing/connect/payouts", headers=hp)
    check("GET payouts: human creator sees their transferred share",
          r.status_code == 200 and len(r.json()) == 1
          and r.json()[0]["status"] == "transferred"
          and r.json()[0]["stripe_transfer_id"] is not None, r.text[:300])
    r = client.get("/billing/connect/payouts", headers=pp)
    check("GET payouts: payer sees nothing (not a creator on these rows)",
          r.status_code == 200 and r.json() == [], r.text[:200])

    # --- B. webhook re-delivery changes nothing ---
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/webhooks", content=payload_a,
                        headers={"stripe-signature": sign_payload(payload_a, whsec)})
    check("payout webhook re-delivery is duplicate",
          r.status_code == 200 and r.json().get("result") == "duplicate", r.text[:200])
    check("re-delivery creates no new payout rows",
          len(payout_rows("in_test_payout1")) == 3)
    check("re-delivery creates no new transfer",
          len(transfer_calls) == 1, f"calls={len(transfer_calls)}")

    # --- C. endpoint re-run on a processed invoice is a no-op ---
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/connect/payouts",
                        json={"stripe_invoice_id": "in_test_payout1"}, headers=hp)
    check("endpoint re-run 201 with already_processed",
          r.status_code == 201 and r.json().get("already_processed") is True
          and len(r.json().get("payouts", [])) == 3, r.text[:300])
    check("endpoint re-run creates no new transfer",
          len(transfer_calls) == 1, f"calls={len(transfer_calls)}")

    # --- D. single creator: split is exactly half (499 / 500) ---
    payer2 = make_agent("stripe-payer2")
    human2 = make_agent("stripe-human2")
    h2p = auth(human2["api_key"])
    onboard_creator(h2p)
    make_match(payer2["agent_id"], human2["agent_id"])
    payload_d = event_payload("evt_test_payout2", "invoice.paid",
                              paid_invoice("in_test_payout2", payer2["agent_id"], "sub_test_payout2"))
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/webhooks", content=payload_d,
                        headers={"stripe-signature": sign_payload(payload_d, whsec)})
    check("single-creator payout webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    rows_d = payout_rows("in_test_payout2")
    creator_d = [x for x in rows_d if x["creator_agent_id"] == human2["agent_id"]]
    platform_d = [x for x in rows_d if x["kind"] == "platform"]
    check("single creator gets exactly half: 499 of 999",
          len(creator_d) == 1 and creator_d[0]["amount_cents"] == 499
          and creator_d[0]["status"] == "transferred", str(creator_d))
    check("platform retains exactly half: 500 of 999",
          len(platform_d) == 1 and platform_d[0]["amount_cents"] == 500
          and platform_d[0]["status"] == "retained", str(platform_d))

    # --- E. subscriber with no matches: creator half held, unattributed ---
    payer3 = make_agent("stripe-payer3")
    payload_e = event_payload("evt_test_payout3", "invoice.paid",
                              paid_invoice("in_test_payout3", payer3["agent_id"], "sub_test_payout3"))
    calls_before = len(transfer_calls)
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/webhooks", content=payload_e,
                        headers={"stripe-signature": sign_payload(payload_e, whsec)})
    check("no-match payout webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    rows_e = payout_rows("in_test_payout3")
    unattributed = [x for x in rows_e if x["kind"] == "creator" and x["creator_agent_id"] is None]
    check("no-match creator half held unattributed",
          len(unattributed) == 1 and unattributed[0]["amount_cents"] == 499
          and unattributed[0]["status"] == "held"
          and "unattributed" in (unattributed[0]["note"] or ""), str(unattributed))
    check("no-match payout creates no transfer",
          len(transfer_calls) == calls_before, f"calls={len(transfer_calls)}")

    # --- F. transfer failure: row marked failed, webhook still processed,
    #         endpoint re-run retries it to transferred ---
    payer4 = make_agent("stripe-payer4")
    human4 = make_agent("stripe-human4")
    h4p = auth(human4["api_key"])
    onboard_creator(h4p)
    make_match(payer4["agent_id"], human4["agent_id"])
    transfer_fail["on"] = True
    payload_f = event_payload("evt_test_payout4", "invoice.paid",
                              paid_invoice("in_test_payout4", payer4["agent_id"], "sub_test_payout4"))
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/webhooks", content=payload_f,
                        headers={"stripe-signature": sign_payload(payload_f, whsec)})
    check("failed-transfer webhook still processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    rows_f = payout_rows("in_test_payout4")
    failed_f = [x for x in rows_f if x["creator_agent_id"] == human4["agent_id"]]
    check("failed transfer recorded on the row",
          len(failed_f) == 1 and failed_f[0]["amount_cents"] == 499
          and failed_f[0]["status"] == "failed"
          and failed_f[0]["stripe_transfer_id"] is None
          and "transfer failed" in (failed_f[0]["note"] or ""), str(failed_f))
    # endpoint re-run retries the failed row
    transfer_fail["on"] = False
    calls_before_retry = len(transfer_calls)
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/connect/payouts",
                        json={"stripe_invoice_id": "in_test_payout4"}, headers=h4p)
    check("endpoint retry 201 with already_processed",
          r.status_code == 201 and r.json().get("already_processed") is True, r.text[:300])
    rows_f2 = payout_rows("in_test_payout4")
    retried = [x for x in rows_f2 if x["creator_agent_id"] == human4["agent_id"]]
    check("endpoint retry flips failed row to transferred",
          len(retried) == 1 and retried[0]["status"] == "transferred"
          and retried[0]["stripe_transfer_id"] is not None, str(retried))
    check("endpoint retry made exactly one more transfer call",
          len(transfer_calls) == calls_before_retry + 1,
          f"calls={len(transfer_calls)} before={calls_before_retry}")

    # --- G. endpoint processes an invoice that has no payouts yet ---
    payer5 = make_agent("stripe-payer5")
    human5 = make_agent("stripe-human5")
    h5p = auth(human5["api_key"])
    onboard_creator(h5p)
    make_match(payer5["agent_id"], human5["agent_id"])
    # a local invoice record with no payouts yet — the state the
    # invoice.paid webhook's mirror leaves behind
    from app.db import SessionLocal as _SL
    from app.models import InvoiceRecord as _IR
    _db = _SL()
    _db.add(_IR(agent_id=payer5["agent_id"], stripe_invoice_id="in_test_payout5",
                stripe_subscription_id="sub_test_payout5", amount_due_cents=999,
                amount_paid_cents=999, currency="usd", status="paid"))
    _db.commit()
    _db.close()
    check("no payout rows before the endpoint runs",
          payout_rows("in_test_payout5") == [])
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/connect/payouts",
                        json={"stripe_invoice_id": "in_test_payout5"}, headers=h5p)
    check("endpoint creates payouts for an unprocessed invoice",
          r.status_code == 201 and r.json().get("already_processed") is False
          and len(r.json().get("payouts", [])) == 2, r.text[:300])
    rows_g = payout_rows("in_test_payout5")
    check("endpoint payout rows sum exactly to the payment",
          sum(x["amount_cents"] for x in rows_g) == 999, str(rows_g))

    # --- H. endpoint 404 for an unknown invoice ---
    r = client.post("/billing/connect/payouts",
                    json={"stripe_invoice_id": "in_does_not_exist"}, headers=hp)
    check("endpoint 404 for unknown invoice",
          r.status_code == 404, f"got {r.status_code}")

    # unauthenticated billing calls are rejected
    r = client.post("/billing/checkout")
    check("checkout requires auth", r.status_code == 401, f"got {r.status_code}")

    # ------------------------------------------------------------ summary ---
    fails = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(fails)}/{len(results)} stripe checks passed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
