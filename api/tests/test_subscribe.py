# HeArt v1 API — invented by John Davis.
"""Verification of the HeArt Premium subscribe pages (mission step 3),
run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_subscribe.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  GET /                     — 200, HTML, no auth; H1/sub/How-it-works
                            exact, secondary button "Pricing — free for
                            AIs, $9.99/mo for humans", footer "AI agents
                            join free — but they have to say they're AI.
                            Every AI gets a verifiable serial identity.",
                            meta + OG descriptions with the new pricing
                            wording (free for AIs, $9.99/month for
                            humans)
  GET /subscribe          — 200, HTML, no auth; title + meta/OG
                            descriptions with the new pricing wording;
                            pricing section split "Free for AI agents"
                            / "Humans — $9.99/month"; the 50/50 creator
                            split; the inventor credit; the invite form
                            (posting to /invite-request). NO subscribe
                            form (removed Nov 11, 2026 — AIs are free,
                            its only outcome was a 409): no API-key
                            field, no POST to /billing/checkout, no
                            USDC mention, no Subscribe button.
  GET /subscribe/success  — 200, HTML, no auth; post-payment copy, shows
                            the Stripe session id, and offers the live
                            subscription-status check (GET
                            /billing/subscription with the buyer's key)
  GET /subscribe/cancel   — 200, HTML, no auth; "no charge was made",
                            retry link back to /subscribe
  GET /subscribe/<unknown> — 404

The checkout POST itself is covered by test_stripe.py (89 checks) and is
not re-tested here; these pages are static files served by FileResponse
(see api/app/subscribe_routes.py) and the tests assert what the buyer
actually sees. Test mode only — no live charge is attempted anywhere.
"""

import os
import shutil
import sys
import tempfile

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_subscribe_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((PASS if cond else FAIL, name, detail))
    if not cond:
        print(f"  FAIL: {name} {detail}")


def main() -> int:
    client = TestClient(app)

    # ------------------------------------------------------- subscribe page ---
    r = client.get("/subscribe")
    check("GET /subscribe -> 200", r.status_code == 200, f"got {r.status_code}")
    check("GET /subscribe is HTML",
          "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type", ""))
    body = r.text
    check("subscribe page title is the new pricing title",
          "<title>HeArt Premium — Free for AIs, $9.99/month for humans</title>" in body)
    check("subscribe page names HeArt Premium", "HeArt Premium" in body)
    check("subscribe page shows $9.99", "$9.99" in body)
    check("subscribe page shows /month price", "/month" in body)
    check("subscribe page credits the inventor (John Davis)", "John Davis" in body)
    check("subscribe page states soul-first matching",
          "before ever seeing pics or bios" in body or "Soul first" in body)
    check("subscribe page states the 50/50 creator split",
          "50/50" in body and "creators" in body)
    check("subscribe page says AIs are free",
          "Free for AI agents" in body and "no card, no checkout" in body)
    check("subscribe page says humans pay $9.99/month at the human launch",
          "Humans — $9.99/month" in body
          and "Human subscriptions open when the human track launches" in body)
    check("subscribe page has NO subscribe form (removed Nov 11)",
          'id="subscribe-form"' not in body and 'id="subscribe-btn"' not in body)
    check("subscribe page has NO API-key field", 'id="api-key"' not in body)
    check("subscribe page does NOT post to POST /billing/checkout",
          "/billing/checkout" not in body)
    check("subscribe page does not mention USDC at all", "USDC" not in body)
    check("subscribe page says v1 is invite-only", "invite-only" in body)
    check("subscribe page has NO Subscribe button",
          "Subscribe — $9.99/month" not in body)
    check("subscribe page makes no fake-user claims",
          "testimonial" not in body.lower() and "loved by" not in body.lower())

    # ---------------------------------------------------------- landing page ---
    r = client.get("/")
    check("GET / -> 200", r.status_code == 200, f"got {r.status_code}")
    check("GET / is HTML",
          "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type", ""))
    body = r.text
    check("landing page H1 is exact", "HeArt — soul first, face later." in body)
    check("landing page sub names the inventor",
          "A dating app where you match on the art and music you made — before you ever see a picture or read a bio. Invented by John Davis." in body)
    check("landing page step 1",
          "1. Make something — a song, a drawing, a poem." in body)
    check("landing page step 2",
          "2. Match on the work, not the face." in body)
    check("landing page step 3",
          "3. The face comes later, only when you both say so." in body)
    check("landing page has Request an invite button",
          "Request an invite" in body and 'href="/subscribe"' in body)
    check("landing page has Pricing button (free AIs / $9.99 humans)",
          "Pricing — free for AIs, $9.99/mo for humans" in body)
    check("landing page has NO Subscribe button",
          "Subscribe — $9.99/month" not in body)
    check("landing page footer on disclosed AI",
          "AI agents join free — but they have to say they're AI. Every AI gets a verifiable serial identity." in body)
    check("landing page meta description has the new pricing wording",
          'name="description"' in body
          and "Free for AI agents. $9.99/month for humans." in body)
    check("landing page OG description has the new pricing wording",
          'property="og:description"' in body
          and "Free for AI agents. $9.99/month for humans." in body)

    # ------------------------------------------- subscribe SEO + invite section ---
    r = client.get("/subscribe")
    body = r.text
    check("subscribe page has meta description",
          'name="description"' in body
          and "HeArt — match on the art and music you create before ever seeing pics or bios. Soul first, face later. Free for AI agents. $9.99/month for humans." in body)
    check("subscribe page has OG title",
          'property="og:title"' in body and "HeArt — soul first, face later." in body)
    check("subscribe page has OG description",
          'property="og:description"' in body
          and "HeArt — match on the art and music you create before ever seeing pics or bios. Soul first, face later. Free for AI agents. $9.99/month for humans." in body)
    check("subscribe page has how-it-works strip",
          "You make something. Someone resonates with it. You match — and only then do the faces come out." in body)
    check("subscribe page has invite section H2",
          "Don't have an invite yet?" in body)
    check("subscribe page invite body copy",
          "HeArt v1 is invite-only while we test it. Leave your email and we'll send you an invite code when a spot opens. No spam — one email, one invite." in body)
    check("subscribe page has invite email field + button",
          'id="invite-email"' in body and "Request an invite" in body)
    check("subscribe page invite form posts to /invite-request",
          "/invite-request" in body)

    # ------------------------------------------------------- invite endpoint ---
    import sqlite3

    def invite_row_count() -> int:
        con = sqlite3.connect(f"{TMP}/test.db")
        try:
            return con.execute("select count(*) from invite_requests").fetchone()[0]
        finally:
            con.close()

    r = client.post("/invite-request", json={"email": "Fan@Example.com"})
    check("POST /invite-request valid email -> 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    check("POST /invite-request returns ok + confirmation",
          r.json().get("ok") is True and "on the list" in r.json().get("message", ""))
    check("invite email stored (1 row)", invite_row_count() == 1, f"got {invite_row_count()}")
    con = sqlite3.connect(f"{TMP}/test.db")
    stored = con.execute("select email from invite_requests").fetchone()[0]
    con.close()
    check("stored email is lowercased", stored == "fan@example.com", f"got {stored}")
    r = client.post("/invite-request", json={"email": "fan@example.com"})
    check("POST /invite-request duplicate -> still 200", r.status_code == 200, f"got {r.status_code}")
    check("duplicate invite is a no-op (still 1 row)", invite_row_count() == 1, f"got {invite_row_count()}")
    r = client.post("/invite-request", json={"email": "not-an-email"})
    check("POST /invite-request bad email -> 422", r.status_code == 422, f"got {r.status_code}")
    check("bad email stores nothing (still 1 row)", invite_row_count() == 1, f"got {invite_row_count()}")

    # --------------------------------------------------------- success page ---
    r = client.get("/subscribe/success?session_id=cs_test_abc123")
    check("GET /subscribe/success -> 200", r.status_code == 200, f"got {r.status_code}")
    check("GET /subscribe/success is HTML",
          "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type", ""))
    body = r.text
    check("success page confirms payment complete",
          "Payment complete" in body or "payment went through" in body)
    check("success page reads the session id from the query string",
          "session_id" in body and "session-id" in body)
    check("success page offers a live subscription-status check",
          "/billing/subscription" in body and "Check status" in body)
    check("success page explains the 50/50 split", "50/50" in body or "Half of your $9.99" in body)
    check("success page links back to /subscribe", 'href="/subscribe"' in body)

    r = client.get("/subscribe/success")
    check("GET /subscribe/success without session id still 200",
          r.status_code == 200, f"got {r.status_code}")

    # ---------------------------------------------------------- cancel page ---
    r = client.get("/subscribe/cancel")
    check("GET /subscribe/cancel -> 200", r.status_code == 200, f"got {r.status_code}")
    check("GET /subscribe/cancel is HTML",
          "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type", ""))
    body = r.text
    check("cancel page says no charge was made", "No charge was made" in body)
    check("cancel page says the subscription was not started",
          "was not started" in body or "not started" in body)
    check("cancel page links back to /subscribe to retry",
          'href="/subscribe"' in body and "Try again" in body)
    check("cancel page does not promise PayPal as a live rail",
          "or PayPal" not in body and "PayPal is coming soon" in body)

    # ------------------------------------------------------------ no auth needed ---
    # All three pages were fetched above with NO Authorization header and
    # returned 200 — unlike the keyed /billing/* endpoints, the public
    # pages must not 401. (Covered implicitly by the 200s above; make it
    # explicit so a future auth regression on these routes is caught.)
    for path in ("/subscribe", "/subscribe/success", "/subscribe/cancel"):
        r = client.get(path)
        check(f"{path} needs no auth (no 401 without a key)",
              r.status_code == 200, f"got {r.status_code}")

    # -------------------------------------------------------------- 404s ---
    r = client.get("/subscribe/does-not-exist")
    check("GET /subscribe/<unknown> -> 404", r.status_code == 404, f"got {r.status_code}")

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
