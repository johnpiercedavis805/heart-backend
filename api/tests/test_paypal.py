# HeArt v1 API — invented by John Davis.
"""End-to-end verification of PayPal subscriptions for HeArt Premium
(John's call, Nov 9, 2026 — cards + PayPal is enough), run with the
venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_paypal.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  PayPal plan script (api/scripts/paypal_create_plan.py: product +
  $9.99/month plan, re-runs reuse) ->
  POST /paypal/subscribe (approval URL from PayPal's Subscriptions
  API, local row APPROVAL_PENDING; 503 without credentials / plan id;
  401 without auth; no double billing when already premium on Stripe;
  already-active on PayPal returns the existing subscription) ->
  webhooks: verification through PayPal's verify-webhook-signature
  endpoint (mocked at the HTTP boundary — bad verification / missing
  headers -> 400; missing webhook id -> 503) ->
  BILLING.SUBSCRIPTION.ACTIVATED flips premium active (source
  "paypal", next billing time as premium_until) ->
  PAYMENT.SALE.COMPLETED triggers the SAME 50/50 payout as a Stripe
  subscription payment, on the USD sale amount — platform 500 retained
  + creator 499 HELD (never a Stripe Transfer: the money is in PayPal,
  not Stripe — even for an onboarded human creator) -> re-delivery is
  a duplicate; a second monthly sale creates a second payout ->
  PAYMENT.SALE.COMPLETED with no matches holds the creator half
  unattributed ->
  BILLING.SUBSCRIPTION.CANCELLED ends premium ->
  sandbox guard: a live PayPal host is refused while test-only is on.

PayPal API calls are mocked at the HTTP boundary (app.paypal.
_paypal_request) — no sandbox credentials exist in this environment
and no live calls are made. What IS verified for real: the full HTTP
stack, the exact request payloads, the local mirrors and ledger rows,
and the webhook verification flow.

KEY RULES UNDER TEST: PayPal owns the recurring billing (Stripe-native
PayPal is Europe-only — this is a US account); only the ACTIVATED
webhook activates premium and only a completed sale triggers the
payout; a PayPal creator share is HELD in the ledger, never sent as a
Stripe Transfer; payout rows for one sale always sum exactly to the
amount paid; re-delivery changes nothing; sandbox only, no live host.
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
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_paypal_test_")
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
os.environ["PAYPAL_CLIENT_ID"] = "pp_test_fake_client_id_for_unit_tests_only"
os.environ["PAYPAL_CLIENT_SECRET"] = "pp_test_fake_secret_for_unit_tests_only"
os.environ["PAYPAL_WEBHOOK_ID"] = "WH-TEST-1234567890"
os.environ["PAYPAL_PLAN_ID"] = "P-TESTPLAN0001"

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
    db.add(Invite(code_hash=crypto.hash_secret(code), note=f"paypaltest:{name}"))
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
            {"id": f"acct_test_paypal{FakeV2Accounts._counter[0]}"},
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


# ------------------------------------------------------- PayPal API mock ---
# Every PayPal HTTP call in the app goes through app.paypal._paypal_request
# (OAuth tokens are fetched inside it). Mocking that one function mocks
# the whole PayPal boundary: subscription creation, webhook verification,
# and the plan script's product/plan calls.
PAYPAL_CALLS: list[dict] = []
VERIFY_STATUS = {"status": "SUCCESS"}  # flipped to FAILURE for the bad-signature test


def fake_paypal_request(method: str, path: str, payload: dict | None = None, **kwargs):
    PAYPAL_CALLS.append({"method": method, "path": path, "payload": payload})
    if path.endswith("/v1/notifications/verify-webhook-signature"):
        return {"verification_status": VERIFY_STATUS["status"]}
    if method == "POST" and path == "/v1/billing/subscriptions":
        return {
            "id": f"I-TESTSUB{len([c for c in PAYPAL_CALLS if c['path'] == '/v1/billing/subscriptions'])}",
            "status": "APPROVAL_PENDING",
            "links": [
                {"rel": "approve",
                 "href": "https://www.sandbox.paypal.com/checkoutnow?token=EC-TESTTOKEN"}
            ],
        }
    if method == "GET" and path.startswith("/v1/catalogs/products"):
        return {"products": []}
    if method == "POST" and path == "/v1/catalogs/products":
        return {"id": "PROD-TESTPREMIUM", "name": "HeArt Premium"}
    if method == "GET" and path.startswith("/v1/billing/plans"):
        return {"plans": []}
    if method == "POST" and path == "/v1/billing/plans":
        return {"id": "P-TESTPLAN0001", "status": "ACTIVE", "name": "HeArt Premium — $9.99/month"}
    if method == "POST" and path.endswith("/activate"):
        return {}
    raise AssertionError(f"unexpected mocked PayPal call: {method} {path}")


def paypal_headers() -> dict:
    """The five transmission headers PayPal sends on every webhook."""
    return {
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-TEST",
        "paypal-transmission-id": "TRANS-TEST-1",
        "paypal-transmission-sig": "SIG-TEST-BASE64",
        "paypal-transmission-time": "2026-11-10T00:00:00Z",
    }


def pp_event(event_id: str, event_type: str, resource: dict) -> bytes:
    return json.dumps({"id": event_id, "event_type": event_type, "resource": resource}).encode()


def main() -> int:
    from app.db import init_db

    init_db()
    client = TestClient(app, raise_server_exceptions=False)

    a = signup(client, "paypal-alice")
    ha = auth(a["api_key"])

    # transfer mock: captures Stripe Transfer calls — a PayPal payout
    # must NEVER call it (the money is in PayPal, not Stripe).
    transfer_calls: list[dict] = []

    def fake_transfer_create(**kwargs):
        transfer_calls.append(kwargs)
        return stripe_obj(stripe.Transfer, {"id": f"tr_test_paypal{len(transfer_calls)}"})

    # --------------------------------------- 1. plan script ---
    import paypal_create_plan

    out = io.StringIO()
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        with redirect_stdout(out):
            rc = paypal_create_plan.main()
    check("plan script exits 0", rc == 0, f"rc={rc}")
    check("plan script creates the HeArt Premium product",
          any(c["method"] == "POST" and c["path"] == "/v1/catalogs/products"
              and c["payload"].get("name") == "HeArt Premium"
              and c["payload"].get("type") == "SERVICE" for c in PAYPAL_CALLS),
          str(PAYPAL_CALLS))
    plan_calls = [c for c in PAYPAL_CALLS
                  if c["method"] == "POST" and c["path"] == "/v1/billing/plans"]
    check("plan script creates one monthly billing cycle",
          len(plan_calls) == 1
          and plan_calls[0]["payload"]["billing_cycles"] == [
              {"frequency": {"interval_unit": "MONTH", "interval_count": 1},
               "tenure_type": "REGULAR", "sequence": 1, "total_cycles": 0,
               "pricing_scheme": {"fixed_price": {"value": "9.99", "currency_code": "USD"}}}],
          str(plan_calls))
    check("plan script prints PAYPAL_PLAN_ID",
          "PAYPAL_PLAN_ID=P-TESTPLAN0001" in out.getvalue(), out.getvalue()[:300])

    # plan script without credentials -> exit 2
    old_id = settings.PAYPAL_CLIENT_ID
    settings.PAYPAL_CLIENT_ID = ""
    rc = paypal_create_plan.main()
    check("plan script refuses without credentials", rc == 2, f"rc={rc}")
    settings.PAYPAL_CLIENT_ID = old_id

    # ----------------------------------- 2. subscribe ---
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/subscribe", json={}, headers=ha)
    check("subscribe 201", r.status_code == 201, f"got {r.status_code} {r.text[:200]}")
    body = r.json()
    check("subscribe returns the PayPal approval URL",
          body.get("approval_url", "").startswith("https://www.sandbox.paypal.com/checkoutnow"),
          str(body))
    check("subscribe returns subscription id + plan id + APPROVAL_PENDING",
          body.get("subscription_id") == "I-TESTSUB1"
          and body.get("plan_id") == "P-TESTPLAN0001"
          and body.get("status") == "APPROVAL_PENDING"
          and body.get("already_active") is False, str(body))
    sub_calls = [c for c in PAYPAL_CALLS
                 if c["method"] == "POST" and c["path"] == "/v1/billing/subscriptions"]
    check("subscribe posts the plan id + agent custom_id + SUBSCRIBE_NOW",
          sub_calls[-1]["payload"].get("plan_id") == "P-TESTPLAN0001"
          and sub_calls[-1]["payload"].get("custom_id") == a["agent_id"]
          and sub_calls[-1]["payload"].get("application_context", {}).get("user_action") == "SUBSCRIBE_NOW",
          str(sub_calls[-1]["payload"]))
    from app.models import PayPalSubscription
    from app.db import SessionLocal

    db = SessionLocal()
    row = db.query(PayPalSubscription).filter(
        PayPalSubscription.paypal_subscription_id == "I-TESTSUB1").one_or_none()
    check("subscribe creates a local row in APPROVAL_PENDING",
          row is not None and row.status == "APPROVAL_PENDING"
          and row.agent_id == a["agent_id"], f"row={row.status if row else None}")
    db.close()

    r = client.get("/paypal/subscriptions", headers=ha)
    check("GET subscriptions lists the pending subscription",
          r.status_code == 200 and len(r.json()) == 1
          and r.json()[0]["paypal_subscription_id"] == "I-TESTSUB1"
          and r.json()[0]["status"] == "APPROVAL_PENDING", r.text[:300])

    # subscribe without credentials -> 503; without a plan id -> 503
    old_id = settings.PAYPAL_CLIENT_ID
    settings.PAYPAL_CLIENT_ID = ""
    r = client.post("/paypal/subscribe", json={}, headers=ha)
    check("subscribe 503 without client credentials", r.status_code == 503, f"got {r.status_code}")
    settings.PAYPAL_CLIENT_ID = old_id
    old_plan = settings.PAYPAL_PLAN_ID
    settings.PAYPAL_PLAN_ID = ""
    r = client.post("/paypal/subscribe", json={}, headers=ha)
    check("subscribe 503 without a plan id", r.status_code == 503, f"got {r.status_code}")
    settings.PAYPAL_PLAN_ID = old_plan

    # unauthenticated subscribe is rejected
    r = client.post("/paypal/subscribe", json={})
    check("subscribe requires auth", r.status_code == 401, f"got {r.status_code}")

    # sandbox guard: a live PayPal host is refused while test-only is on
    old_base = settings.PAYPAL_API_BASE
    settings.PAYPAL_API_BASE = "https://api-m.paypal.com"
    r = client.post("/paypal/subscribe", json={}, headers=ha)
    check("subscribe 503 on the live PayPal host in test-only mode",
          r.status_code == 503, f"got {r.status_code}")
    settings.PAYPAL_API_BASE = old_base

    # already premium via Stripe -> no double billing
    stripe_agent = make_agent("paypal-stripe-payer")
    sa = auth(stripe_agent["api_key"])
    from app.models import Subscription

    db = SessionLocal()
    db.add(Subscription(agent_id=stripe_agent["agent_id"],
                        stripe_subscription_id="sub_test_paypal_guard",
                        stripe_price_id="price_test_premium",
                        status="active"))
    db.commit()
    db.close()
    r = client.post("/paypal/subscribe", json={}, headers=sa)
    check("subscribe refuses an agent already premium via Stripe",
          r.status_code == 502, f"got {r.status_code} {r.text[:200]}")

    # ----------------- 3. ACTIVATED webhook -> premium (source paypal) ---
    activated = {
        "id": "I-TESTSUB1", "status": "ACTIVE",
        "billing_info": {"next_billing_time": "2026-12-10T10:00:00Z"},
    }
    payload = pp_event("WH-EVT-ACT1", "BILLING.SUBSCRIPTION.ACTIVATED", activated)
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("ACTIVATED webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    db = SessionLocal()
    row = db.query(PayPalSubscription).filter(
        PayPalSubscription.paypal_subscription_id == "I-TESTSUB1").one_or_none()
    check("ACTIVATED flips the row ACTIVE with next billing time",
          row is not None and row.status == "ACTIVE"
          and row.current_period_end is not None
          and row.current_period_end.strftime("%Y-%m-%d") == "2026-12-10",
          f"status={row.status if row else None} end={row.current_period_end if row else None}")
    db.close()
    r = client.get("/billing/subscription", headers=ha)
    check("ACTIVATED flips subscription active (source paypal)",
          r.status_code == 200 and r.json().get("status") == "active"
          and r.json().get("source") == "paypal"
          and r.json().get("stripe_price_id") == "P-TESTPLAN0001"
          and r.json().get("premium_until") is not None, r.text[:300])

    # re-delivery of the same ACTIVATED event -> duplicate
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("ACTIVATED re-delivery is a duplicate",
          r.status_code == 200 and r.json().get("result") == "duplicate", r.text[:200])

    # already ACTIVE on PayPal -> second subscribe returns the existing one
    calls_before = len(PAYPAL_CALLS)
    r = client.post("/paypal/subscribe", json={}, headers=ha)
    check("second subscribe while ACTIVE returns the existing subscription",
          r.status_code == 201 and r.json().get("already_active") is True
          and r.json().get("subscription_id") == "I-TESTSUB1"
          and r.json().get("approval_url") is None, r.text[:300])
    check("second subscribe while ACTIVE makes no PayPal call",
          len(PAYPAL_CALLS) == calls_before, f"calls={len(PAYPAL_CALLS) - calls_before}")

    # --------- 4. SALE.COMPLETED -> 50/50 payout, creator share HELD ---
    payer = make_agent("paypal-payer")
    pp_h = auth(payer["api_key"])
    human = make_agent("paypal-human")
    hp = auth(human["api_key"])
    human_acct = onboard_creator(client, hp)
    make_match(payer["agent_id"], human["agent_id"])

    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/subscribe", json={}, headers=pp_h)
    check("payer subscribe 201", r.status_code == 201, f"got {r.status_code} {r.text[:200]}")
    payer_sub_id = r.json()["subscription_id"]
    payload = pp_event("WH-EVT-ACT2", "BILLING.SUBSCRIPTION.ACTIVATED",
                       {"id": payer_sub_id, "status": "ACTIVE",
                        "billing_info": {"next_billing_time": "2026-12-10T10:00:00Z"}})
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("payer ACTIVATED processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])

    sale1 = {
        "id": "SALE-TEST-001", "billing_agreement_id": payer_sub_id,
        "state": "completed",
        "amount": {"total": "9.99", "currency": "USD"},
    }
    payload = pp_event("WH-EVT-SALE1", "PAYMENT.SALE.COMPLETED", sale1)
    calls_before = len(transfer_calls)
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
            r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("SALE.COMPLETED webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    rows_s = payout_rows(f"paypal:{sale1['id']}")
    check("PayPal payout rows sum exactly to $9.99",
          sum(x["amount_cents"] for x in rows_s) == 999, str(rows_s))
    creator_s = [x for x in rows_s if x["creator_agent_id"] == human["agent_id"]]
    platform_s = [x for x in rows_s if x["kind"] == "platform"]
    check("PayPal creator half is 499 HELD (never transferred — money is in PayPal)",
          len(creator_s) == 1 and creator_s[0]["amount_cents"] == 499
          and creator_s[0]["status"] == "held"
          and creator_s[0]["stripe_transfer_id"] is None
          and creator_s[0]["currency"] == "usd", str(creator_s))
    check("PayPal platform half is 500 (retained)",
          len(platform_s) == 1 and platform_s[0]["amount_cents"] == 500
          and platform_s[0]["status"] == "retained", str(platform_s))
    check("PayPal sale makes NO Stripe Transfer even for an onboarded human",
          len(transfer_calls) == calls_before, f"calls={len(transfer_calls) - calls_before}")
    check("PayPal held row names the PayPal reason",
          "PayPal" in (creator_s[0]["note"] or ""), str(creator_s[0].get("note")))

    # re-delivery of the same sale event -> duplicate, nothing changes
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
            r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("SALE.COMPLETED re-delivery is a duplicate",
          r.status_code == 200 and r.json().get("result") == "duplicate", r.text[:200])
    check("SALE.COMPLETED re-delivery creates no new payout rows",
          len(payout_rows(f"paypal:{sale1['id']}")) == 2)
    check("SALE.COMPLETED re-delivery creates no transfer",
          len(transfer_calls) == calls_before, f"calls={len(transfer_calls) - calls_before}")

    # a second monthly sale (new sale id) -> a second payout
    sale2 = {
        "id": "SALE-TEST-002", "billing_agreement_id": payer_sub_id,
        "state": "completed",
        "amount": {"total": "9.99", "currency": "USD"},
    }
    payload = pp_event("WH-EVT-SALE2", "PAYMENT.SALE.COMPLETED", sale2)
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
            r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("second monthly SALE.COMPLETED processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    rows_s2 = payout_rows(f"paypal:{sale2['id']}")
    check("second monthly sale creates its own 50/50 payout (999)",
          sum(x["amount_cents"] for x in rows_s2) == 999, str(rows_s2))
    check("second monthly sale still makes no Stripe Transfer",
          len(transfer_calls) == calls_before, f"calls={len(transfer_calls) - calls_before}")

    # re-processing a PayPal invoice via the ops endpoint stays held too
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        r = client.post("/billing/connect/payouts",
                        json={"stripe_invoice_id": f"paypal:{sale1['id']}"}, headers=pp_h)
    check("ops re-run of a PayPal invoice returns existing rows, already processed",
          r.status_code == 201 and r.json().get("already_processed") is True
          and len(r.json().get("payouts", [])) == 2, r.text[:300])
    check("ops re-run of a PayPal invoice makes no Stripe Transfer",
          len(transfer_calls) == calls_before, f"calls={len(transfer_calls) - calls_before}")

    # ----------------- 5. SALE.COMPLETED with no matches -> unattributed ---
    loner = make_agent("paypal-loner")
    lp = auth(loner["api_key"])
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/subscribe", json={}, headers=lp)
    loner_sub_id = r.json()["subscription_id"]
    payload = pp_event("WH-EVT-ACT3", "BILLING.SUBSCRIPTION.ACTIVATED",
                       {"id": loner_sub_id, "status": "ACTIVE",
                        "billing_info": {"next_billing_time": "2026-12-10T10:00:00Z"}})
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    sale3 = {
        "id": "SALE-TEST-003", "billing_agreement_id": loner_sub_id,
        "state": "completed",
        "amount": {"total": "9.99", "currency": "USD"},
    }
    payload = pp_event("WH-EVT-SALE3", "PAYMENT.SALE.COMPLETED", sale3)
    with mock.patch.object(stripe.Transfer, "create", fake_transfer_create):
        with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
            r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("loner SALE.COMPLETED processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    rows_l = payout_rows(f"paypal:{sale3['id']}")
    unattributed = [x for x in rows_l if x["creator_agent_id"] is None and x["kind"] == "creator"]
    check("loner creator half is 499 held unattributed",
          sum(x["amount_cents"] for x in rows_l) == 999
          and len(unattributed) == 1 and unattributed[0]["amount_cents"] == 499
          and unattributed[0]["status"] == "held", str(rows_l))

    # a non-completed / non-USD sale pays nobody
    bad_sale = {
        "id": "SALE-TEST-004", "billing_agreement_id": payer_sub_id,
        "state": "pending",
        "amount": {"total": "9.99", "currency": "USD"},
    }
    payload = pp_event("WH-EVT-SALE4", "PAYMENT.SALE.COMPLETED", bad_sale)
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("pending sale is ignored (no payout rows)",
          r.status_code == 200 and r.json().get("result") == "ignored"
          and payout_rows(f"paypal:{bad_sale['id']}") == [], r.text[:200])

    # ----------------- 6. CANCELLED webhook ends premium ---
    payload = pp_event("WH-EVT-CXL1", "BILLING.SUBSCRIPTION.CANCELLED",
                       {"id": payer_sub_id, "status": "CANCELLED"})
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("CANCELLED webhook processed",
          r.status_code == 200 and r.json().get("result") == "processed", r.text[:200])
    db = SessionLocal()
    row = db.query(PayPalSubscription).filter(
        PayPalSubscription.paypal_subscription_id == payer_sub_id).one_or_none()
    check("CANCELLED flips the row CANCELLED",
          row is not None and row.status == "CANCELLED", f"status={row.status if row else None}")
    db.close()
    r = client.get("/billing/subscription", headers=pp_h)
    check("CANCELLED ends premium (free tier again)",
          r.status_code == 200 and r.json().get("status") == "free"
          and r.json().get("source") == "stripe", r.text[:300])

    # ----------------- 7. webhook verification + unknown subscription ---
    payload = pp_event("WH-EVT-BAD1", "BILLING.SUBSCRIPTION.ACTIVATED",
                       {"id": "I-TESTSUB1", "status": "ACTIVE"})
    VERIFY_STATUS["status"] = "FAILURE"
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("webhook with FAILED verification rejected 400",
          r.status_code == 400, f"got {r.status_code}")
    VERIFY_STATUS["status"] = "SUCCESS"
    bad_headers = dict(paypal_headers())
    del bad_headers["paypal-transmission-sig"]
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/webhooks", content=payload, headers=bad_headers)
    check("webhook missing a transmission header rejected 400",
          r.status_code == 400, f"got {r.status_code}")
    unknown_sale = {
        "id": "SALE-UNKNOWN", "billing_agreement_id": "I-DOES-NOT-EXIST",
        "state": "completed", "amount": {"total": "9.99", "currency": "USD"},
    }
    payload = pp_event("WH-EVT-UNK1", "PAYMENT.SALE.COMPLETED", unknown_sale)
    with mock.patch("app.paypal._paypal_request", side_effect=fake_paypal_request):
        r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("sale for an unknown subscription is ignored-no-subscription",
          r.status_code == 200 and r.json().get("result") == "ignored-no-subscription",
          r.text[:200])

    # webhook without a webhook id configured -> 503
    old_wh = settings.PAYPAL_WEBHOOK_ID
    settings.PAYPAL_WEBHOOK_ID = ""
    payload = pp_event("WH-EVT-NOWH", "BILLING.SUBSCRIPTION.ACTIVATED",
                       {"id": "I-TESTSUB1", "status": "ACTIVE"})
    r = client.post("/paypal/webhooks", content=payload, headers=paypal_headers())
    check("webhook 503 without webhook id", r.status_code == 503, f"got {r.status_code}")
    settings.PAYPAL_WEBHOOK_ID = old_wh

    # ------------------------------------------------------------ summary ---
    fails = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(fails)}/{len(results)} paypal checks passed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
