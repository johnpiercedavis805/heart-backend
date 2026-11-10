# HeArt v1 API — invented by John Davis.
"""Verification of the funnel observability build (Nov 10, 2026),
run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_funnel.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  GET /admin/funnel         — 403 without the admin token (or a wrong
                             one); 200 with it; counters + recent
                             events + started_at; no PII anywhere
  funnel.record / snapshot  — counters increment per event, recent
                             events are key + timestamp only
  checkout created/failed   — POST /billing/checkout with the Stripe
                             SDK boundary mocked: success counts
                             stripe.checkout.created, a Stripe error
                             counts stripe.checkout.failed
  Stripe webhooks           — REAL HMAC-SHA256 signature verification
                             (same helper as test_stripe.py):
                             checkout.session.completed counts
                             stripe.webhook.received/processed and
                             stripe.checkout.completed; a re-delivered
                             event counts stripe.webhook.duplicate;
                             an unknown event type counts
                             stripe.webhook.ignored
  subscription flips        — customer.subscription.created (active)
                             counts stripe.subscription.free_to_premium;
                             customer.subscription.deleted counts
                             stripe.subscription.premium_to_free
  PayPal webhooks           — signature verification mocked at the
                             boundary (same pattern as test_paypal.py):
                             ACTIVATED counts paypal.webhook.processed
                             + paypal.subscription.free_to_premium;
                             a re-delivery counts paypal.webhook.duplicate;
                             CANCELLED counts
                             paypal.subscription.premium_to_free; an
                             unknown type counts paypal.webhook.ignored

No live keys, no live charges — Stripe signatures are computed locally
and PayPal verification is mocked. The counters are in-process (see
app/funnel.py); this file resets them at the start so the counts it
asserts are exact.
"""

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_funnel_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake_key_for_unit_tests_only"
os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_fake_key_for_unit_tests_only"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret_for_unit_tests_only"
os.environ["STRIPE_PRICE_PREMIUM_MONTHLY"] = "price_test_premium"
os.environ["STRIPE_CHECKOUT_SUCCESS_URL"] = "https://example.invalid/checkout/success?session_id={CHECKOUT_SESSION_ID}"
os.environ["STRIPE_CHECKOUT_CANCEL_URL"] = "https://example.invalid/checkout/cancel"
os.environ["PAYPAL_CLIENT_ID"] = "fake-paypal-client-id"
os.environ["PAYPAL_CLIENT_SECRET"] = "fake-paypal-client-secret"
os.environ["PAYPAL_PLAN_ID"] = "P-test-plan"
os.environ["PAYPAL_WEBHOOK_ID"] = "WH-test-webhook"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import funnel  # noqa: E402
from app.main import app  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((PASS if cond else FAIL, name, detail))
    if not cond:
        print(f"  FAIL: {name} {detail}")


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


def sign_payload(payload: bytes, secret: str) -> str:
    """Stripe's webhook signature scheme, exactly as Stripe signs it:
    header 't=<unix ts>,v1=<HMAC-SHA256(secret, '<ts>.<payload>')>'."""
    ts = int(time.time())
    sig = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={sig}"


def stripe_event(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps(
        {"id": event_id, "type": event_type, "data": {"object": obj}}
    ).encode()


def post_stripe_webhook(client: TestClient, event_id: str, event_type: str, obj: dict):
    payload = stripe_event(event_id, event_type, obj)
    return client.post(
        "/billing/webhooks",
        content=payload,
        headers={"stripe-signature": sign_payload(payload, "whsec_test_secret_for_unit_tests_only")},
    )


def counters(client: TestClient) -> dict:
    r = client.get("/admin/funnel", headers={"X-Admin-Token": "test-admin-token"})
    assert r.status_code == 200, f"/admin/funnel failed: {r.status_code} {r.text}"
    return r.json()["counters"]


def main() -> int:
    from app.db import init_db

    init_db()  # create tables in the isolated test DB (the lifespan would
    # also do this under `with TestClient(app)`, but this test drives the
    # DB directly for agent setup — same pattern as test_stripe.py)
    client = TestClient(app, raise_server_exceptions=False)
    funnel.reset()

    # ------------------------------------------------------- endpoint auth ---
    r = client.get("/admin/funnel")
    check("GET /admin/funnel without token -> 403", r.status_code == 403, f"got {r.status_code}")
    r = client.get("/admin/funnel", headers={"X-Admin-Token": "wrong-token"})
    check("GET /admin/funnel with wrong token -> 403", r.status_code == 403, f"got {r.status_code}")
    r = client.get("/admin/funnel", headers={"X-Admin-Token": "test-admin-token"})
    check("GET /admin/funnel with admin token -> 200", r.status_code == 200, f"got {r.status_code}")
    body = r.json()
    check("funnel response has counters/recent/started_at",
          set(body.keys()) == {"counters", "recent", "started_at"}, str(sorted(body.keys())))
    check("funnel counters start empty", body["counters"] == {}, str(body["counters"]))
    check("funnel recent starts empty", body["recent"] == [])
    check("funnel started_at is an ISO timestamp", "T" in body["started_at"])

    # ------------------------------------------------- record / snapshot ---
    funnel.record("checkout.created", provider="stripe")
    funnel.record("checkout.created", provider="stripe")
    snap = funnel.snapshot()
    check("funnel.record increments the counter",
          snap["counters"].get("stripe.checkout.created") == 2, str(snap["counters"]))
    check("funnel recent holds key + timestamp only",
          all(set(e.keys()) == {"event", "at"} for e in snap["recent"])
          and snap["recent"][-1]["event"] == "stripe.checkout.created",
          str(snap["recent"][-1:]))
    check("funnel snapshot carries no PII (no emails, keys, or ids)",
          "@" not in json.dumps(snap) and "api_key" not in json.dumps(snap).lower(),
          "PII-shaped value in snapshot")
    # A flip that stays on one side of the premium boundary is silent.
    funnel.record_subscription_flip("active", "past_due", provider="stripe")
    check("active -> past_due is not a flip (both premium)",
          funnel.snapshot()["counters"].get("stripe.subscription.free_to_premium") is None)
    funnel.record_subscription_flip(None, "incomplete", provider="stripe")
    check("free -> incomplete is not a flip (both free)",
          funnel.snapshot()["counters"].get("stripe.subscription.free_to_premium") is None)

    # ---------------------------------------------- checkout created/failed ---
    agent = make_agent("funnel-buyer")
    with mock.patch("app.billing.create_checkout_session",
                    return_value={"checkout_url": "https://checkout.stripe.test/s/cs_test_1",
                                  "session_id": "cs_test_1",
                                  "payment_method_types": ["card"]}) as m:
        r = client.post("/billing/checkout", json={"currency": "usd"}, headers=auth(agent["api_key"]))
        check("POST /billing/checkout (mocked) -> 200", r.status_code == 200, f"got {r.status_code} {r.text}")
        check("mocked checkout called once", m.call_count == 1)
    c = counters(client)
    check("checkout.created counted (2 unit + 1 route)",
          c.get("stripe.checkout.created") == 3, str(c))
    from app import billing as billing_mod
    with mock.patch("app.billing.create_checkout_session",
                    side_effect=billing_mod.BillingError("Stripe Checkout session creation failed: boom")):
        r = client.post("/billing/checkout", json={"currency": "usd"}, headers=auth(agent["api_key"]))
        check("POST /billing/checkout on Stripe error -> 502", r.status_code == 502, f"got {r.status_code}")
    c = counters(client)
    check("checkout.failed counted on Stripe error",
          c.get("stripe.checkout.failed") == 1, str(c))

    # --------------------------------------------------- Stripe webhooks ---
    # checkout.session.completed: received + processed + completed.
    r = post_stripe_webhook(client, "evt_checkout_1", "checkout.session.completed",
                            {"id": "cs_test_1", "customer": "cus_test_1",
                             "metadata": {"agent_id": agent["agent_id"]}})
    check("checkout.session.completed webhook -> 200 processed",
          r.status_code == 200 and r.json().get("result") == "processed",
          f"got {r.status_code} {r.text}")
    c = counters(client)
    check("stripe.webhook.received counted", c.get("stripe.webhook.received") == 1, str(c))
    check("stripe.webhook.processed counted", c.get("stripe.webhook.processed") == 1, str(c))
    check("stripe.checkout.completed counted", c.get("stripe.checkout.completed") == 1, str(c))
    # Re-delivery of the same event id: duplicate, nothing re-processed.
    r = post_stripe_webhook(client, "evt_checkout_1", "checkout.session.completed",
                            {"id": "cs_test_1", "customer": "cus_test_1",
                             "metadata": {"agent_id": agent["agent_id"]}})
    check("re-delivered Stripe event -> duplicate",
          r.status_code == 200 and r.json().get("result") == "duplicate",
          f"got {r.status_code} {r.text}")
    c = counters(client)
    check("stripe.webhook.duplicate counted", c.get("stripe.webhook.duplicate") == 1, str(c))
    check("duplicate did not double-count checkout.completed",
          c.get("stripe.checkout.completed") == 1, str(c))
    # Unknown event type: ignored.
    r = post_stripe_webhook(client, "evt_unknown_1", "payment_intent.succeeded", {})
    check("unknown Stripe event -> ignored",
          r.status_code == 200 and r.json().get("result") == "ignored",
          f"got {r.status_code} {r.text}")
    c = counters(client)
    check("stripe.webhook.ignored counted", c.get("stripe.webhook.ignored") == 1, str(c))

    # ------------------------------------------------- subscription flips ---
    sub_obj = {"id": "sub_test_1", "status": "active",
               "customer": "cus_test_1",
               "metadata": {"agent_id": agent["agent_id"]},
               "items": {"data": [{"price": {"id": "price_test_premium"}}]},
               "current_period_end": int(time.time()) + 2592000,
               "cancel_at_period_end": False}
    r = post_stripe_webhook(client, "evt_sub_created_1", "customer.subscription.created", sub_obj)
    check("subscription.created (active) webhook -> processed",
          r.status_code == 200 and r.json().get("result") == "processed",
          f"got {r.status_code} {r.text}")
    c = counters(client)
    check("stripe.subscription.free_to_premium counted",
          c.get("stripe.subscription.free_to_premium") == 1, str(c))
    # A second active sync (e.g. invoice.paid's belt-and-braces path)
    # must NOT count a second flip — already premium.
    r = post_stripe_webhook(client, "evt_sub_updated_1", "customer.subscription.updated", sub_obj)
    c = counters(client)
    check("repeat active sync is not a second flip",
          c.get("stripe.subscription.free_to_premium") == 1, str(c))
    sub_obj_deleted = dict(sub_obj, status="canceled")
    r = post_stripe_webhook(client, "evt_sub_deleted_1", "customer.subscription.deleted", sub_obj_deleted)
    check("subscription.deleted webhook -> processed",
          r.status_code == 200 and r.json().get("result") == "processed",
          f"got {r.status_code} {r.text}")
    c = counters(client)
    check("stripe.subscription.premium_to_free counted",
          c.get("stripe.subscription.premium_to_free") == 1, str(c))

    # --------------------------------------------------- PayPal webhooks ---
    from app.db import SessionLocal
    from app.models import PayPalSubscription

    pp_agent = make_agent("funnel-paypal-buyer")
    db = SessionLocal()
    db.add(PayPalSubscription(agent_id=pp_agent["agent_id"],
                              paypal_subscription_id="I-PPTEST123",
                              paypal_plan_id="P-test-plan",
                              status="APPROVAL_PENDING"))
    db.commit()
    db.close()

    def pp_headers() -> dict:
        return {
            "paypal-auth-algo": "SHA256withRSA",
            "paypal-cert-url": "https://api.sandbox.paypal.com/certs",
            "paypal-transmission-id": "tx-test-1",
            "paypal-transmission-sig": "sig-test",
            "paypal-transmission-time": "2026-11-10T00:00:00Z",
        }

    def pp_event(event_id: str, event_type: str, resource: dict) -> bytes:
        return json.dumps(
            {"id": event_id, "event_type": event_type, "resource": resource}
        ).encode()

    with mock.patch("app.paypal.verify_paypal_webhook", return_value=True):
        r = client.post("/paypal/webhooks",
                        content=pp_event("WH-EVT-1", "BILLING.SUBSCRIPTION.ACTIVATED",
                                         {"id": "I-PPTEST123",
                                          "billing_info": {"next_billing_time": "2026-12-10T00:00:00Z"}}),
                        headers=pp_headers())
        check("PayPal ACTIVATED webhook -> processed",
              r.status_code == 200 and r.json().get("result") == "processed",
              f"got {r.status_code} {r.text}")
        c = counters(client)
        check("paypal.webhook.processed counted", c.get("paypal.webhook.processed") == 1, str(c))
        check("paypal.subscription.free_to_premium counted",
              c.get("paypal.subscription.free_to_premium") == 1, str(c))
        # Re-delivery: duplicate.
        r = client.post("/paypal/webhooks",
                        content=pp_event("WH-EVT-1", "BILLING.SUBSCRIPTION.ACTIVATED",
                                         {"id": "I-PPTEST123",
                                          "billing_info": {"next_billing_time": "2026-12-10T00:00:00Z"}}),
                        headers=pp_headers())
        check("re-delivered PayPal event -> duplicate",
              r.status_code == 200 and r.json().get("result") == "duplicate",
              f"got {r.status_code} {r.text}")
        c = counters(client)
        check("paypal.webhook.duplicate counted", c.get("paypal.webhook.duplicate") == 1, str(c))
        # CANCELLED: premium -> free.
        r = client.post("/paypal/webhooks",
                        content=pp_event("WH-EVT-2", "BILLING.SUBSCRIPTION.CANCELLED",
                                         {"id": "I-PPTEST123"}),
                        headers=pp_headers())
        check("PayPal CANCELLED webhook -> processed",
              r.status_code == 200 and r.json().get("result") == "processed",
              f"got {r.status_code} {r.text}")
        c = counters(client)
        check("paypal.subscription.premium_to_free counted",
              c.get("paypal.subscription.premium_to_free") == 1, str(c))
        # Unknown type: ignored.
        r = client.post("/paypal/webhooks",
                        content=pp_event("WH-EVT-3", "BILLING.PLAN.CREATED",
                                         {"id": "I-PPTEST123"}),
                        headers=pp_headers())
        check("unknown PayPal event -> ignored",
              r.status_code == 200 and r.json().get("result") == "ignored",
              f"got {r.status_code} {r.text}")
        c = counters(client)
        check("paypal.webhook.ignored counted", c.get("paypal.webhook.ignored") == 1, str(c))
        check("paypal.webhook.received counted (4 events)",
              c.get("paypal.webhook.received") == 4, str(c))

    # Bad PayPal signature verification: 400, nothing counted as received.
    with mock.patch("app.paypal.verify_paypal_webhook", return_value=False):
        before = counters(client).get("paypal.webhook.received")
        r = client.post("/paypal/webhooks",
                        content=pp_event("WH-EVT-9", "BILLING.SUBSCRIPTION.ACTIVATED",
                                         {"id": "I-PPTEST123"}),
                        headers=pp_headers())
        check("unverified PayPal webhook -> 400", r.status_code == 400, f"got {r.status_code}")
        check("unverified PayPal webhook not counted as received",
              counters(client).get("paypal.webhook.received") == before)

    # ------------------------------------------------------------ final ---
    body = client.get("/admin/funnel", headers={"X-Admin-Token": "test-admin-token"}).json()
    check("recent events list is bounded (<= 100)",
          len(body["recent"]) <= 100, f"got {len(body['recent'])}")
    check("every recent event is a known funnel key",
          all(e["event"].split(".")[0] in ("stripe", "paypal") for e in body["recent"]))

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
