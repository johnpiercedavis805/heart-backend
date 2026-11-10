# HeArt v1 API — invented by John Davis.
"""Verification of the HeArt Premium subscribe pages (mission step 3),
run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_subscribe.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  GET /subscribe          — 200, HTML, no auth; carries the $9.99 price,
                            the inventor credit, and the subscribe form
                            (API-key field + card-only checkout, posting
                            to POST /billing/checkout with a Bearer key)
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
    check("subscribe page names HeArt Premium", "HeArt Premium" in body)
    check("subscribe page shows $9.99", "$9.99" in body)
    check("subscribe page shows /month price", "/month" in body)
    check("subscribe page credits the inventor (John Davis)", "John Davis" in body)
    check("subscribe page states soul-first matching",
          "before ever seeing pics or bios" in body or "Soul first" in body)
    check("subscribe page states the 50/50 creator split",
          "50/50" in body and "creators" in body)
    check("subscribe page has an API-key field", 'id="api-key"' in body)
    check("subscribe page posts to POST /billing/checkout",
          "/billing/checkout" in body and "Bearer" in body)
    check("subscribe page offers card (USD) only — no USDC radio",
          'value="usd"' in body and 'value="usdc"' not in body)
    check("subscribe page does not mention USDC at all", "USDC" not in body)
    check("subscribe page says v1 is invite-only", "invite-only" in body)
    check("subscribe page has a Subscribe button", "Subscribe" in body)
    check("subscribe page makes no fake-user claims",
          "testimonial" not in body.lower() and "loved by" not in body.lower())

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
