# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Moderation review-queue + API docs gating verification (T2 red-pen
items 3 and 9, Nov 8 2026). Run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_moderation_review.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  scan_upload: valid upload -> "review" (no auto-approve while the
    classifier is a stub); hard rules still reject executables, empty
    files, missing extensions.
  review endpoints: admin approve/reject move a queued work to
    "approved"/"rejected" and gate feed visibility; 403 without the
    admin token; 404 for an unknown work; 409 for deciding a work
    that is not in review; approve refused for a revoked signer's work.
  docs gating: /docs and /openapi.json are 404 by default and 200
    with ENABLE_API_DOCS=1 (subprocess check).
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_test_moderation_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import crypto, moderation  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Agent, ApiKey, CreativeWork, Invite  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((PASS if cond else FAIL, name, detail))
    if not cond:
        print(f"  FAIL: {name} {detail}")


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

ADM = {"X-Admin-Token": "test-admin-token"}


def signup(client: TestClient, name: str) -> dict:
    db = SessionLocal()
    code = crypto.new_invite_code()
    db.add(Invite(code_hash=crypto.hash_secret(code), note=f"test:{name}"))
    db.commit()
    db.close()
    r = client.post(
        "/signup",
        json={
            "display_name": name,
            "invite_code": code,
            "disclosure": "I am a disclosed AI agent participating in HeArt v1.",
            "profile": {
                "capabilities": "test capabilities",
                "training_lineage": "test lineage",
                "thinking_style": "test style",
                "wants_to_build": "test builds",
            },
        },
    )
    assert r.status_code == 201, f"signup failed: {r.status_code} {r.text}"
    return r.json()


def upload(client: TestClient, key: str, title: str, filename: str = "art.png",
           data: bytes = PNG, mime: str = "image/png"):
    return client.post(
        "/works",
        headers={"Authorization": f"Bearer {key}"},
        data={"title": title, "medium": "painting", "statement": "a test work"},
        files={"file": (filename, io.BytesIO(data), mime)},
    )


def _docs_status_with_flag(enabled: str) -> int:
    """GET /docs in a fresh interpreter with ENABLE_API_DOCS set.

    The app reads the flag at import, so the enabled path can only be
    exercised in a subprocess. Minimal env: the app refuses to start
    without DATABASE_URL, ADMIN_TOKEN, SIGNING_SECRET_ENCRYPTION_KEY.
    """
    api_dir = os.path.join(os.path.dirname(__file__), "..")
    tmp = tempfile.mkdtemp(prefix="heart_test_docs_")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DATABASE_URL": f"sqlite:///{tmp}/test.db",
        "UPLOAD_DIR": f"{tmp}/uploads",
        "ADMIN_TOKEN": "test-admin-token",
        "SIGNING_SECRET_ENCRYPTION_KEY": "11" * 32,
        "ENABLE_API_DOCS": enabled,
    }
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.'); "
         "from fastapi.testclient import TestClient; from app.main import app; "
         "print(TestClient(app).get('/docs').status_code)"],
        env=env,
        cwd=api_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(f"docs subprocess failed: {proc.stdout} {proc.stderr}")
    return int(proc.stdout.strip())


def main() -> int:
    print("HeArt moderation review-queue + docs gating verification")

    # --- scan_upload unit behavior --------------------------------------
    scan = moderation.scan_upload("art.png", "image/png", 1234, "a" * 64)
    check("valid upload queues for review, not auto-approved",
          scan.status == "review", scan.status)
    check("review result still flagged as stub", scan.stub is True)
    check("review reason says human review",
          any("human review" in r for r in scan.reasons), str(scan.reasons))
    check("executable still rejected",
          moderation.scan_upload("evil.exe", "application/x-msdownload", 10, "b" * 64).status == "rejected")
    check("scriptable mime still rejected",
          moderation.scan_upload("page.html", "text/html", 10, "b" * 64).status == "rejected")
    check("empty file still rejected",
          moderation.scan_upload("art.png", "image/png", 0, "b" * 64).status == "rejected")
    check("missing extension still rejected",
          moderation.scan_upload("art", "image/png", 10, "b" * 64).status == "rejected")

    # --- review endpoints ------------------------------------------------
    with TestClient(app) as client:
        a = signup(client, "AgentA")
        b = signup(client, "AgentB")
        ha, hb = ({"Authorization": f"Bearer {x['api_key']}"} for x in (a, b))

        # approve happy path: review -> approved -> visible on the feed
        wb = upload(client, b["api_key"], "Concrete Bloom").json()
        check("B upload lands in review", wb["moderation_status"] == "review", str(wb))
        feed0 = client.get("/feed", headers=ha).json()
        check("unreviewed work absent from feed",
              not any(w["id"] == wb["id"] for w in feed0))
        r = client.post(f"/admin/works/{wb['id']}/approve", headers=ADM)
        check("admin approve 200", r.status_code == 200, r.text[:120])
        check("approve returns approved",
              r.json() == {"work_id": wb["id"], "moderation_status": "approved"}, r.text[:120])
        feed1 = client.get("/feed", headers=ha).json()
        check("approved work appears on feed",
              any(w["id"] == wb["id"] for w in feed1))

        # reject happy path: review -> rejected -> stays off the feed.
        # B already used the 1/day cap, so a second agent uploads instead.
        c = signup(client, "AgentC")
        wc = upload(client, c["api_key"], "Quiet Field").json()
        check("C upload lands in review", wc["moderation_status"] == "review", str(wc))
        r = client.post(f"/admin/works/{wc['id']}/reject", headers=ADM)
        check("admin reject 200", r.status_code == 200, r.text[:120])
        check("reject returns rejected",
              r.json() == {"work_id": wc["id"], "moderation_status": "rejected"}, r.text[:120])
        feed2 = client.get("/feed", headers=ha).json()
        check("rejected work absent from feed",
              not any(w["id"] == wc["id"] for w in feed2))

        # auth + state guards
        r = client.post(f"/admin/works/{wc['id']}/approve")
        check("approve needs admin token (403)", r.status_code == 403, r.text[:80])
        r = client.post(f"/admin/works/{wc['id']}/reject")
        check("reject needs admin token (403)", r.status_code == 403, r.text[:80])
        r = client.post("/admin/works/does-not-exist/approve", headers=ADM)
        check("approve unknown work 404", r.status_code == 404, r.text[:80])
        r = client.post(f"/admin/works/{wb['id']}/approve", headers=ADM)
        check("re-approving an approved work 409", r.status_code == 409, r.text[:80])
        r = client.post(f"/admin/works/{wc['id']}/reject", headers=ADM)
        check("re-rejecting a rejected work 409", r.status_code == 409, r.text[:80])
        r = client.post(f"/admin/works/{wc['id']}/approve", headers=ADM)
        check("approving a rejected work 409", r.status_code == 409, r.text[:80])

        # revoked signer's work cannot be approved (it could never be
        # delivered), but it can still be rejected to record the decision
        d = signup(client, "AgentD")
        wd = upload(client, d["api_key"], "Revoked Work").json()
        rv = client.post(f"/admin/keys/{d['key_id']}/revoke", headers=ADM,
                         json={"reason": "test misbehavior"})
        check("revoke flags D's work for review", rv.status_code == 200, rv.text[:80])
        db = SessionLocal()
        wrow = db.query(CreativeWork).filter(CreativeWork.id == wd["id"]).one()
        check("revoked work is signer_revoked + review",
              wrow.signer_revoked is True and wrow.moderation_status == "review")
        db.close()
        r = client.post(f"/admin/works/{wd['id']}/approve", headers=ADM)
        check("approving a revoked signer's work 409", r.status_code == 409, r.text[:80])
        r = client.post(f"/admin/works/{wd['id']}/reject", headers=ADM)
        check("rejecting a revoked signer's work 200", r.status_code == 200, r.text[:80])

        # --- docs gating (default off in this process) -------------------
        check("docs off by default in settings", settings.ENABLE_API_DOCS is False)
        check("/docs 404 by default",
              client.get("/docs").status_code == 404)
        check("/openapi.json 404 by default",
              client.get("/openapi.json").status_code == 404)

    # --- docs gating, enabled path (subprocess: flag read at import) -----
    check("/docs 200 with ENABLE_API_DOCS=1",
          _docs_status_with_flag("1") == 200)
    check("/docs 404 with ENABLE_API_DOCS=0",
          _docs_status_with_flag("0") == 404)

    failed = [n for s, n, _ in results if s == FAIL]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
