# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""End-to-end verification of the human track (M1 accounts/auth + M2 Stripe
Identity verification), run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_humans.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  M1: signup validation (bad email, short password, bad/used invite, AI-pool
      invite rejected, duplicate email, case normalisation) -> Argon2id
      password storage (PHC string, never plaintext) -> login (wrong
      password / unknown email give the SAME 401) -> /me (no password hash
      in the payload) -> logout/revoke -> session rotate (24h grace, then
      dead; an expired session can still rotate) -> session expiry ->
      cross-kind rejection (an AI key gets 401 on human routes; a human
      token gets 401 on AI routes).
  M2: verify start (the exact Stripe Identity call arguments: document
      type, driving_license only, require_id_number + require_matching_selfie
      + require_live_capture, client_reference_id = human id) -> live-key
      guard (503 in test-only mode) -> already-verified 409, suspended 403
      -> webhooks: REAL signature verification (payloads signed with
      HMAC-SHA256 exactly as Stripe signs them — bad/missing signature is
      rejected 400), verified (adult -> active; minor -> verified but
      never active; missing DOB -> failed), requires_input (error code
      stored), canceled / redacted (back to unverified), idempotency (a
      re-delivered event is processed once), unknown session ignored, and
      the older-session-verifies-after-a-newer-started fallback via
      client_reference_id.

Stripe Identity calls are mocked at the SDK boundary (no test key exists
in this environment and test mode does not actually process checks — see
docs/human-verification-stripe-identity.md). What IS verified for real:
the full HTTP stack, the exact SDK call arguments, the local mirrors, and
webhook signature crypto.

KEY RULES UNDER TEST: test mode only (a live key is refused); webhooks
verify the Stripe signature before touching state; HeArt NEVER stores ID
images, document numbers, full DOB, or SSN digits of any kind — the
human_verifications table has no column for them, and the verified webhook
payload in these tests deliberately carries PII (name, DOB, address,
last-4 SSN) to prove none of it lands in the database; a verified minor
is never activated; human and AI auth never cross.
"""

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_humans_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/heart.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake_key_for_unit_tests_only"
os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_fake_key_for_unit_tests_only"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_billing_secret_for_unit_tests_only"
os.environ["STRIPE_IDENTITY_WEBHOOK_SECRET"] = "whsec_test_identity_secret_for_unit_tests_only"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import stripe  # noqa: E402
from app import crypto, human_auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((PASS if cond else FAIL, name, detail))
    if not cond:
        print(f"  FAIL: {name} {detail}")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


def mint_human_invite(note: str = "humanstest") -> str:
    """A fresh human-pool invite code, inserted directly (the HTTP signup
    route is rate-limited to 10/hour per IP)."""
    from app.db import SessionLocal
    from app.models import HumanInvite

    code = crypto.new_invite_code()
    db = SessionLocal()
    db.add(HumanInvite(code_hash=crypto.hash_secret(code), note=note))
    db.commit()
    db.close()
    return code


def mint_ai_invite() -> str:
    """An AI-pool invite code — must NEVER work for a human signup."""
    from app.db import SessionLocal
    from app.models import Invite

    code = crypto.new_invite_code()
    db = SessionLocal()
    db.add(Invite(code_hash=crypto.hash_secret(code), note="humanstest:ai-pool"))
    db.commit()
    db.close()
    return code


def make_human(email: str, password: str = "correct-horse-battery-staple") -> dict:
    """Create a human directly in the DB (same rows / hashing as HTTP
    signup, via the real hash_password + issue_session) — the HTTP signup
    route is rate-limited, and most tests need a human without burning
    the signup budget. Returns {human_id, token, email}."""
    from app.db import SessionLocal
    from app.models import Human

    db = SessionLocal()
    human = Human(
        email=email.lower(),
        display_name=email.split("@")[0],
        password_hash=human_auth.hash_password(password),
        status="pending_verification",
    )
    db.add(human)
    db.flush()
    _row, plaintext = human_auth.issue_session(db, human.id)
    db.commit()
    out = {"human_id": human.id, "token": plaintext, "email": email.lower()}
    db.close()
    return out


def make_ai_agent(name: str) -> dict:
    """An AI agent + key, for the cross-kind tests."""
    from app import auth as ai_auth
    from app.db import SessionLocal
    from app.models import Agent, AgentProfile

    db = SessionLocal()
    agent = Agent(
        display_name=name,
        disclosure="I am a disclosed AI agent participating in HeArt v1.",
        status="active",
    )
    db.add(agent)
    db.flush()
    db.add(
        AgentProfile(
            agent_id=agent.id,
            capabilities="test",
            training_lineage="test",
            thinking_style="test",
            wants_to_build="test",
        )
    )
    _row, plaintext = ai_auth.issue_key(db, agent.id)
    db.commit()
    out = {"agent_id": agent.id, "api_key": plaintext}
    db.close()
    return out


def db_human(human_id: str) -> dict:
    from app.db import SessionLocal
    from app.models import Human

    db = SessionLocal()
    row = db.get(Human, human_id)
    out = {"status": row.status, "password_hash": row.password_hash, "email": row.email}
    db.close()
    return out


def db_verification(human_id: str) -> dict | None:
    from app.db import SessionLocal
    from app.models import HumanVerification

    db = SessionLocal()
    row = db.get(HumanVerification, human_id)
    if row is None:
        db.close()
        return None
    out = {
        "vendor": row.vendor,
        "vendor_ref": row.vendor_ref,
        "status": row.status,
        "is_18_plus": row.is_18_plus,
        "verified_at": row.verified_at,
        "last_error_code": row.last_error_code,
    }
    db.close()
    return out


def set_human_status(human_id: str, status: str) -> None:
    from app.db import SessionLocal
    from app.models import Human

    db = SessionLocal()
    row = db.get(Human, human_id)
    row.status = status
    db.commit()
    db.close()


def backdate_session(token: str, **kwargs) -> None:
    """Move a human session's expires_at / grace_until (for expiry + grace tests)."""
    from app.db import SessionLocal
    from app.models import HumanSession

    db = SessionLocal()
    row = (
        db.query(HumanSession)
        .filter(HumanSession.key_hash == crypto.hash_secret(token))
        .one()
    )
    for k, v in kwargs.items():
        setattr(row, k, v)
    db.commit()
    db.close()


def verified_session_obj(session_id: str, human_id: str, dob: dict | None) -> dict:
    """A Stripe verification_session object as the verified webhook carries
    it — DELIBERATELY carrying PII (name, DOB, address, last-4 SSN) so the
    tests can prove none of it lands in HeArt's database."""
    obj = {
        "id": session_id,
        "object": "verification_session",
        "status": "verified",
        "client_reference_id": human_id,
        "verified_outputs": {
            "first_name": "Test",
            "last_name": "Human",
            "address": {
                "line1": "1 Test Way",
                "city": "Goleta",
                "state": "CA",
                "postal_code": "93117",
                "country": "US",
            },
            "id_number": "1234",
            "id_number_type": "us_ssn",
            "sex": "male",
        },
    }
    if dob is not None:
        obj["verified_outputs"]["dob"] = dob
    else:
        obj["verified_outputs"].pop("dob", None)
    return obj


def post_identity_webhook(client: TestClient, event_id: str, event_type: str, obj: dict, secret: str | None = None):
    payload = event_payload(event_id, event_type, obj)
    headers = {}
    if secret is not None:
        headers["stripe-signature"] = sign_payload(payload, secret)
    return client.post("/humans/verify/webhook", content=payload, headers=headers)


def main() -> int:
    from app.db import init_db

    init_db()
    client = TestClient(app, raise_server_exceptions=False)

    # --- fake Stripe Identity sessions (mocked at the SDK boundary) ---
    identity_calls: list[dict] = []

    def fake_identity_create(**kwargs):
        identity_calls.append(kwargs)
        n = len(identity_calls)
        return SimpleNamespace(
            id=f"vs_test_{n:03d}",
            url=f"https://verify.stripe.com/test/session_{n:03d}",
            client_secret=f"vs_test_{n:03d}_secret",
        )

    # ============================================================ M1: signup ===
    code_alice = mint_human_invite("alice")
    r = client.post(
        "/humans/signup",
        json={"email": "not-an-email", "password": "long-enough-password-1", "display_name": "X", "invite_code": code_alice},
    )
    check("signup: bad email -> 422", r.status_code == 422, f"{r.status_code} {r.text[:120]}")
    r = client.post(
        "/humans/signup",
        json={"email": "alice@example.invalid", "password": "short", "display_name": "Alice", "invite_code": code_alice},
    )
    check("signup: short password -> 422", r.status_code == 422, f"{r.status_code} {r.text[:120]}")
    r = client.post(
        "/humans/signup",
        json={"email": "alice@example.invalid", "password": "long-enough-password-1", "display_name": "Alice", "invite_code": "hi_nope"},
    )
    check("signup: bad invite -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    r = client.post(
        "/humans/signup",
        json={"email": "alice@example.invalid", "password": "long-enough-password-1", "display_name": "Alice", "invite_code": mint_ai_invite()},
    )
    check("signup: AI-pool invite code cannot sign up a human -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    r = client.post(
        "/humans/signup",
        json={"email": "alice@example.invalid", "password": "long-enough-password-1", "display_name": "Alice", "invite_code": code_alice},
    )
    check("signup: success -> 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    alice = r.json()
    check("signup: token has the hs_ prefix (never hk_)", alice["session_token"].startswith("hs_"), alice["session_token"][:8])
    check("signup: human_id + expires_at returned", bool(alice.get("human_id")) and bool(alice.get("expires_at")))
    stored = db_human(alice["human_id"])
    check("signup: password stored as an Argon2id PHC string", stored["password_hash"].startswith("$argon2id$"), stored["password_hash"][:20])
    check("signup: plaintext password is never stored", "long-enough-password-1" not in stored["password_hash"])
    check("signup: new human starts pending_verification", stored["status"] == "pending_verification", stored["status"])
    r = client.post(
        "/humans/signup",
        json={"email": "alice@example.invalid", "password": "another-long-password-2", "display_name": "Alice2", "invite_code": mint_human_invite("alice2")},
    )
    check("signup: duplicate email -> 409", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
    r = client.post(
        "/humans/signup",
        json={"email": "bob@example.invalid", "password": "long-enough-password-1", "display_name": "Bob", "invite_code": code_alice},
    )
    check("signup: invite reuse -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    code_carol = mint_human_invite("carol")
    r = client.post(
        "/humans/signup",
        json={"email": "Carol@Example.INVALID", "password": "long-enough-password-1", "display_name": "Carol", "invite_code": code_carol},
    )
    check("signup: mixed-case email succeeds", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
    if r.status_code == 201:
        check("signup: email is stored lowercased", db_human(r.json()["human_id"])["email"] == "carol@example.invalid")

    # ============================================================= M1: login ===
    r = client.post("/humans/login", json={"email": "alice@example.invalid", "password": "wrong-password-long"})
    check("login: wrong password -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r_wrong = r.json()
    r = client.post("/humans/login", json={"email": "nobody@example.invalid", "password": "wrong-password-long"})
    check("login: unknown email -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    check("login: wrong password and unknown email give the SAME 401 (no enumeration)",
          r.json().get("detail") == r_wrong.get("detail"), f"{r_wrong} vs {r.json()}")
    r = client.post("/humans/login", json={"email": "ALICE@EXAMPLE.INVALID", "password": "long-enough-password-1"})
    check("login: correct password (case-insensitive email) -> 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    alice_token_2 = r.json()["session_token"] if r.status_code == 200 else ""
    check("login: fresh token is hs_-prefixed", alice_token_2.startswith("hs_"))
    r = client.get("/humans/me", headers=auth(alice_token_2))
    check("login: fresh token authenticates /me", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

    # =============================================================== M1: /me ===
    r = client.get("/humans/me")
    check("me: no token -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r = client.get("/humans/me", headers=auth("hs_garbage"))
    check("me: garbage token -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r = client.get("/humans/me", headers=auth(alice["session_token"]))
    check("me: valid token -> 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    if r.status_code == 200:
        me = r.json()
        check("me: returns id + email + display name + status", me["human_id"] == alice["human_id"] and me["email"] == "alice@example.invalid" and me["status"] == "pending_verification", str(me)[:160])
        check("me: never returns a password hash", "password_hash" not in me and "password" not in json.dumps(me), str(me)[:160])
        check("me: verification summary included", me["verification_status"] == "unverified" and me["is_18_plus"] is None, str(me)[:160])

    # ================================================ M1: cross-kind rejection ===
    ai = make_ai_agent("cross-kind-ai")
    r = client.get("/humans/me", headers=auth(ai["api_key"]))
    check("cross-kind: AI key (hk_) gets 401 on a human route", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r = client.get("/feed", headers=auth(alice["session_token"]))
    check("cross-kind: human token (hs_) gets 401 on an AI route", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r = client.get("/feed", headers=auth(ai["api_key"]))
    check("cross-kind: AI key still works on its own route", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

    # ============================================ M1: logout / rotate / expiry ===
    bob = make_human("bob@example.invalid")
    r = client.post("/humans/logout", headers=auth(bob["token"]))
    check("logout: -> 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    r = client.get("/humans/me", headers=auth(bob["token"]))
    check("logout: token is dead immediately after", r.status_code == 401, f"{r.status_code} {r.text[:120]}")

    carol = make_human("carol2@example.invalid")
    r = client.post("/humans/sessions/rotate", headers=auth(carol["token"]))
    check("rotate: -> 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    new_token = r.json()["session_token"] if r.status_code == 200 else ""
    check("rotate: new token is hs_-prefixed and different", new_token.startswith("hs_") and new_token != carol["token"])
    r = client.get("/humans/me", headers=auth(new_token))
    check("rotate: new token works", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    r = client.get("/humans/me", headers=auth(carol["token"]))
    check("rotate: old token still works inside the 24h grace", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    backdate_session(carol["token"], grace_until=datetime.now() - timedelta(hours=1))
    r = client.get("/humans/me", headers=auth(carol["token"]))
    check("rotate: old token is dead after the grace expires", r.status_code == 401, f"{r.status_code} {r.text[:120]}")

    dave = make_human("dave@example.invalid")
    backdate_session(dave["token"], expires_at=datetime.now() - timedelta(hours=1))
    r = client.get("/humans/me", headers=auth(dave["token"]))
    check("expiry: expired session -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    check("expiry: the 401 names rotation as the way back", "rotate" in r.json().get("detail", ""), r.json())
    r = client.post("/humans/sessions/rotate", headers=auth(dave["token"]))
    check("expiry: an expired session can still rotate (not a dead end)", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    if r.status_code == 200:
        r2 = client.get("/humans/me", headers=auth(r.json()["session_token"]))
        check("expiry: the rotated token works", r2.status_code == 200, f"{r2.status_code} {r2.text[:120]}")

    # ================================================ M2: verify start (mocked) ===
    eve = make_human("eve@example.invalid")
    r = client.post("/humans/verify")
    check("verify: no auth -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
        r = client.post("/humans/verify", headers=auth(eve["token"]))
    check("verify: start -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 200:
        started = r.json()
        check("verify: returns a session id + hosted URL + client_secret",
              started["session_id"].startswith("vs_") and started["verification_url"].startswith("https://") and bool(started["client_secret"]),
              str(started)[:160])
        check("verify: client_secret is NOT the session id", started["client_secret"] != started["session_id"])
    check("verify: exactly one Stripe Identity session was created", len(identity_calls) == 1, str(len(identity_calls)))
    if identity_calls:
        call = identity_calls[0]
        check("verify: type is document", call.get("type") == "document", str(call)[:200])
        doc_opts = (call.get("options") or {}).get("document") or {}
        check("verify: allowed_types is driving_license ONLY", doc_opts.get("allowed_types") == ["driving_license"], str(doc_opts))
        check("verify: require_id_number is on (last-4 SSN check)", doc_opts.get("require_id_number") is True, str(doc_opts))
        check("verify: require_matching_selfie is on (face match vs the license)", doc_opts.get("require_matching_selfie") is True, str(doc_opts))
        check("verify: require_live_capture is on (camera only, no uploads)", doc_opts.get("require_live_capture") is True, str(doc_opts))
        check("verify: client_reference_id is the human id (no PII)", call.get("client_reference_id") == eve["human_id"], str(call)[:200])
        check("verify: metadata carries only the human id (no PII)", (call.get("metadata") or {}).get("heart_human_id") == eve["human_id"], str(call)[:200])
    vrow = db_verification(eve["human_id"])
    check("verify: local row is pending with the session as vendor_ref",
          vrow is not None and vrow["status"] == "pending" and vrow["vendor_ref"] == "vs_test_001" and vrow["vendor"] == "stripe",
          str(vrow))
    # No PII columns exist on the table at all — the never-store rule is structural.
    from app.db import SessionLocal
    from app.models import HumanVerification as _HV

    cols = {c.name for c in _HV.__table__.columns}
    forbidden = {"dob", "date_of_birth", "id_number", "ssn", "document_number", "document_image",
                 "selfie", "selfie_image", "biometric", "first_name", "last_name", "address"}
    check("verify: human_verifications has NO column for DOB/ID/SSN/selfie/biometrics/name/address",
          not (cols & forbidden), f"forbidden columns present: {cols & forbidden}; columns: {sorted(cols)}")

    # live-key guard: a live key is refused while STRIPE_TEST_MODE_ONLY is on.
    frank = make_human("frank@example.invalid")
    with mock.patch.object(settings, "STRIPE_SECRET_KEY", "sk_live_fake_key_for_unit_tests_only"):
        with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
            r = client.post("/humans/verify", headers=auth(frank["token"]))
    check("verify: live key refused in test-only mode -> 503", r.status_code == 503, f"{r.status_code} {r.text[:160]}")

    # suspended humans cannot start verification.
    grace = make_human("grace@example.invalid")
    set_human_status(grace["human_id"], "suspended")
    with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
        r = client.post("/humans/verify", headers=auth(grace["token"]))
    check("verify: suspended human -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

    # ================================================ M2: webhook signatures ===
    ident_secret = settings.STRIPE_IDENTITY_WEBHOOK_SECRET
    heidi = make_human("heidi@example.invalid")
    with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
        r = client.post("/humans/verify", headers=auth(heidi["token"]))
    heidi_session = r.json()["session_id"] if r.status_code == 200 else "vs_test_002"
    obj_verified = verified_session_obj(heidi_session, heidi["human_id"], {"day": 15, "month": 5, "year": 1990})
    r = client.post("/humans/verify/webhook", content=event_payload("evt_bad_1", "identity.verification_session.verified", obj_verified))
    check("webhook: missing signature -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    r = client.post(
        "/humans/verify/webhook",
        content=event_payload("evt_bad_2", "identity.verification_session.verified", obj_verified),
        headers={"stripe-signature": "t=123,v1=deadbeef"},
    )
    check("webhook: bad signature -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    r = client.post(
        "/humans/verify/webhook",
        content=event_payload("evt_bad_3", "identity.verification_session.verified", obj_verified),
        headers={"stripe-signature": sign_payload(event_payload("evt_bad_3", "identity.verification_session.verified", obj_verified), settings.STRIPE_WEBHOOK_SECRET)},
    )
    check("webhook: signature from the BILLING secret is rejected (separate endpoint secret)", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    # ================================================ M2: verified (adult) ===
    r = post_identity_webhook(client, "evt_verified_heidi", "identity.verification_session.verified", obj_verified, secret=ident_secret)
    check("webhook verified: -> 200 processed", r.status_code == 200 and r.json().get("status") == "processed", f"{r.status_code} {r.text[:160]}")
    vrow = db_verification(heidi["human_id"])
    check("webhook verified: row is verified + 18+ + dated",
          vrow is not None and vrow["status"] == "verified" and vrow["is_18_plus"] is True and vrow["verified_at"] is not None,
          str(vrow))
    check("webhook verified: human becomes active", db_human(heidi["human_id"])["status"] == "active")
    check("webhook verified: vendor_ref is the session that verified them", vrow["vendor_ref"] == heidi_session, str(vrow))
    r = client.get("/humans/verify/status", headers=auth(heidi["token"]))
    check("verify/status: verified + 18+ + date, no vendor_ref exposed",
          r.status_code == 200 and r.json()["status"] == "verified" and r.json()["is_18_plus"] is True
          and r.json()["verified_at"] is not None and "vendor_ref" not in r.json(),
          f"{r.status_code} {r.text[:200]}")
    r = post_identity_webhook(client, "evt_verified_heidi", "identity.verification_session.verified", obj_verified, secret=ident_secret)
    check("webhook: re-delivery of the same event is a duplicate (processed once)",
          r.status_code == 200 and r.json().get("status") == "duplicate", f"{r.status_code} {r.text[:160]}")
    with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
        r = client.post("/humans/verify", headers=auth(heidi["token"]))
    check("verify: already-verified human -> 409", r.status_code == 409, f"{r.status_code} {r.text[:120]}")

    # ============================================ M2: verified (minor) + DOB edge ===
    ivan = make_human("ivan@example.invalid")
    with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
        r = client.post("/humans/verify", headers=auth(ivan["token"]))
    ivan_session = r.json()["session_id"] if r.status_code == 200 else ""
    obj_minor = verified_session_obj(ivan_session, ivan["human_id"], {"day": 1, "month": 1, "year": 2012})
    r = post_identity_webhook(client, "evt_verified_ivan", "identity.verification_session.verified", obj_minor, secret=ident_secret)
    check("webhook verified (minor): -> 200 processed", r.status_code == 200 and r.json().get("status") == "processed", f"{r.status_code} {r.text[:160]}")
    vrow = db_verification(ivan["human_id"])
    check("webhook verified (minor): identity verified but is_18_plus is False",
          vrow is not None and vrow["status"] == "verified" and vrow["is_18_plus"] is False, str(vrow))
    check("webhook verified (minor): human is NEVER activated", db_human(ivan["human_id"])["status"] == "pending_verification")

    judy = make_human("judy@example.invalid")
    with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
        r = client.post("/humans/verify", headers=auth(judy["token"]))
    judy_session = r.json()["session_id"] if r.status_code == 200 else ""
    obj_no_dob = verified_session_obj(judy_session, judy["human_id"], None)
    r = post_identity_webhook(client, "evt_verified_judy", "identity.verification_session.verified", obj_no_dob, secret=ident_secret)
    vrow = db_verification(judy["human_id"])
    check("webhook verified (no DOB): does not count — failed / dob_missing",
          vrow is not None and vrow["status"] == "failed" and vrow["last_error_code"] == "dob_missing", str(vrow))
    check("webhook verified (no DOB): human stays pending_verification", db_human(judy["human_id"])["status"] == "pending_verification")

    # 18th-birthday edge cases, unit level (no webhook needed).
    from app.human_verification import _is_18_plus

    today = date.today()
    check("18+ edge: 18th birthday today -> True",
          _is_18_plus({"day": today.day, "month": today.month, "year": today.year - 18}, on=today) is True)
    check("18+ edge: turns 18 tomorrow -> False",
          _is_18_plus({"day": (today + timedelta(days=1)).day, "month": (today + timedelta(days=1)).month, "year": today.year - 18}, on=today) is False)
    check("18+ edge: no DOB -> None", _is_18_plus(None) is None)
    check("18+ edge: malformed DOB -> None", _is_18_plus({"day": "x", "month": 5, "year": 1990}) is None)

    # ============================== M2: requires_input / canceled / redacted / unknown ===
    karl = make_human("karl@example.invalid")
    with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
        r = client.post("/humans/verify", headers=auth(karl["token"]))
    karl_session = r.json()["session_id"] if r.status_code == 200 else ""
    obj_input = {"id": karl_session, "object": "verification_session", "status": "requires_input",
                 "client_reference_id": karl["human_id"],
                 "last_error": {"code": "document_unverified_other", "reason": "document_unverified_other"}}
    r = post_identity_webhook(client, "evt_input_karl", "identity.verification_session.requires_input", obj_input, secret=ident_secret)
    vrow = db_verification(karl["human_id"])
    check("webhook requires_input: stays pending, machine error code stored",
          r.json().get("status") == "processed" and vrow is not None and vrow["status"] == "pending"
          and vrow["last_error_code"] == "document_unverified_other", f"{r.status_code} {r.text[:120]} {vrow}")

    obj_canceled = {"id": karl_session, "object": "verification_session", "status": "canceled",
                    "client_reference_id": karl["human_id"]}
    r = post_identity_webhook(client, "evt_canceled_karl", "identity.verification_session.canceled", obj_canceled, secret=ident_secret)
    vrow = db_verification(karl["human_id"])
    check("webhook canceled: back to unverified (may retry)",
          r.json().get("status") == "processed" and vrow is not None and vrow["status"] == "unverified", f"{r.text[:120]} {vrow}")

    obj_redacted = {"id": karl_session, "object": "verification_session", "status": "redacted",
                    "client_reference_id": karl["human_id"]}
    r = post_identity_webhook(client, "evt_redacted_karl", "identity.verification_session.redacted", obj_redacted, secret=ident_secret)
    vrow = db_verification(karl["human_id"])
    check("webhook redacted: back to unverified, 18+ cleared",
          r.json().get("status") == "processed" and vrow is not None and vrow["status"] == "unverified" and vrow["is_18_plus"] is None,
          f"{r.text[:120]} {vrow}")

    obj_processing = {"id": karl_session, "object": "verification_session", "status": "processing",
                      "client_reference_id": karl["human_id"]}
    r = post_identity_webhook(client, "evt_processing_karl", "identity.verification_session.processing", obj_processing, secret=ident_secret)
    check("webhook processing: ignored, no state change",
          r.status_code == 200 and r.json().get("status") == "ignored", f"{r.status_code} {r.text[:120]}")

    obj_unknown = verified_session_obj("vs_test_unknown", "00000000-0000-0000-0000-000000000000", {"day": 1, "month": 1, "year": 1990})
    r = post_identity_webhook(client, "evt_unknown", "identity.verification_session.verified", obj_unknown, secret=ident_secret)
    check("webhook: session HeArt cannot tie to any human -> ignored-no-human, still 200",
          r.status_code == 200 and r.json().get("status") == "ignored-no-human", f"{r.status_code} {r.text[:160]}")

    # ============ M2: older session verifies after a newer one was started ===
    leo = make_human("leo@example.invalid")
    with mock.patch.object(stripe.identity.VerificationSession, "create", side_effect=fake_identity_create):
        r1 = client.post("/humans/verify", headers=auth(leo["token"]))
        r2 = client.post("/humans/verify", headers=auth(leo["token"]))
    old_session, new_session = r1.json()["session_id"], r2.json()["session_id"]
    check("verify twice: two distinct Stripe sessions", old_session != new_session, f"{old_session} vs {new_session}")
    check("verify twice: vendor_ref points at the newest session",
          db_verification(leo["human_id"])["vendor_ref"] == new_session)
    obj_old_verified = verified_session_obj(old_session, leo["human_id"], {"day": 2, "month": 2, "year": 1985})
    r = post_identity_webhook(client, "evt_verified_leo_old", "identity.verification_session.verified", obj_old_verified, secret=ident_secret)
    vrow = db_verification(leo["human_id"])
    check("webhook (older session): resolved via client_reference_id, processed",
          r.json().get("status") == "processed" and vrow is not None and vrow["status"] == "verified",
          f"{r.text[:120]} {vrow}")
    check("webhook (older session): vendor_ref now names the session that verified them",
          vrow["vendor_ref"] == old_session, str(vrow))
    check("webhook (older session): human becomes active", db_human(leo["human_id"])["status"] == "active")

    # ============================================================ summary ===
    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print(f"{len(failed)} FAILED:")
        for _, name, detail in failed:
            print(f"  - {name} {detail}")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
