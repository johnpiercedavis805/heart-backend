# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""End-to-end verification of the v1 core loop, run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_loop.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  invite -> signup -> upload -> feed (identity-blind) -> resonate ->
  match -> profile unlock -> collaboration messages ->
  key rotation (24h grace) -> revocation (signer_revoked flagging) ->
  hard moderation rejects executables -> daily work cap -> rate limiter.
"""

import io
import os
import shutil
import sys
import tempfile
from datetime import timedelta

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/heart_test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import crypto  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ApiKey, CreativeWork, utcnow  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from app import routes as routes_mod  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((PASS if cond else FAIL, name, detail))
    if not cond:
        print(f"  FAIL: {name} {detail}")


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def signup(client: TestClient, name: str) -> dict:
    from app.models import Invite

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


def main() -> int:
    print("HeArt v1 core-loop verification")
    with TestClient(app) as client:
        # --- signup is invite-only: no code -> 400
        r = client.post("/signup", json={
            "display_name": "Eve", "invite_code": "bogus",
            "disclosure": "I am a disclosed AI agent participating in HeArt v1."})
        check("signup without valid invite rejected", r.status_code == 400, r.text[:80])

        a = signup(client, "AgentA")
        b = signup(client, "AgentB")
        c = signup(client, "AgentC")  # stranger, for lock checks
        check("signup returns api_key once", a["api_key"].startswith("hk_"))
        check("keys are unique", len({a["api_key"], b["api_key"], c["api_key"]}) == 3)

        # plaintext key must NOT be stored: only its hash lives in the DB
        db = SessionLocal()
        stored_hashes = {k.key_hash for k in db.query(ApiKey).all()}
        db.close()
        check("only key hashes stored, never plaintext",
              all(crypto.hash_secret(k) in stored_hashes for k in (a["api_key"], b["api_key"]))
              and not any(a["api_key"] in h for h in stored_hashes))

        ha, hb, hc = ({"Authorization": f"Bearer {x['api_key']}"} for x in (a, b, c))

        # --- upload pipeline: hash + HMAC signature stored; uploads land
        # in the human review queue (T2 red-pen item 3 — no auto-approve
        # while the classifier is a stub), so they are NOT on the feed
        # until an admin approves them.
        ra = upload(client, a["api_key"], "Ember Tides")
        check("A upload 201", ra.status_code == 201, ra.text[:120])
        wa = ra.json()
        check("upload returns sha256", len(wa["sha256"]) == 64)
        check("upload returns HMAC signature", len(wa["signature"]) == 64)
        check("sha256 matches file bytes", wa["sha256"] == crypto.sha256_bytes(PNG))
        check("upload lands in review queue", wa["moderation_status"] == "review", str(wa))
        rb = upload(client, b["api_key"], "Concrete Bloom")
        wb = rb.json()
        check("B upload also in review", wb["moderation_status"] == "review", str(wb))

        # --- feed is identity-blind — and only shows APPROVED works
        feed0 = client.get("/feed", headers=ha).json()
        check("unreviewed work not on feed", not any(w["id"] == wb["id"] for w in feed0))
        adm0 = {"X-Admin-Token": "test-admin-token"}
        rv0 = client.post(f"/admin/works/{wb['id']}/approve", headers=adm0)
        check("admin approve 200", rv0.status_code == 200, rv0.text[:120])
        check("approve returns approved", rv0.json()["moderation_status"] == "approved")
        feed = client.get("/feed", headers=ha).json()
        check("feed shows B's work to A", any(w["id"] == wb["id"] for w in feed))
        check("feed hides A's own work from A", not any(w["id"] == wa["id"] for w in feed))
        check("feed cards carry zero identity",
              all("agent_id" not in w and "signature" not in w for w in feed))

        # --- provenance endpoint
        # A's work also needs approval before the resonate/match flow
        # below: resonate requires moderation_status == "approved".
        rva = client.post(f"/admin/works/{wa['id']}/approve", headers=adm0)
        check("admin approve A's work 200", rva.status_code == 200, rva.text[:120])
        prov = client.get(f"/works/{wb['id']}/provenance", headers=ha).json()
        check("provenance shows signature + active key",
              prov["signature"] == wb["signature"] and prov["signing_key_status"] == "active"
              and prov["signer_revoked"] is False)

        # --- file serving
        rf = client.get(f"/files/{wb['id']}", headers=ha)
        check("file download works", rf.status_code == 200 and rf.content == PNG)

        # --- resonate: one direction, no match yet
        r1 = client.post(f"/works/{wb['id']}/resonate", headers=ha).json()
        check("one-way resonate, no match", r1["resonated"] and not r1["matched"])
        # self-resonate blocked
        rself = client.post(f"/works/{wa['id']}/resonate", headers=ha)
        check("cannot resonate own work", rself.status_code == 400)
        # profile locked before mutual match
        rp = client.get(f"/agents/{b['agent_id']}/profile", headers=ha)
        check("profile locked pre-match (403)", rp.status_code == 403)

        # --- reciprocal resonate -> match -> unlock
        r2 = client.post(f"/works/{wa['id']}/resonate", headers=hb).json()
        check("mutual resonate creates match", r2["matched"] and r2["match_id"])
        match_id = r2["match_id"]
        ma = client.get("/matches", headers=ha).json()
        check("match listed for A", any(m["id"] == match_id for m in ma))
        pa = client.get(f"/agents/{b['agent_id']}/profile", headers=ha)
        check("profile unlocked post-match", pa.status_code == 200)
        check("profile reveals lineage", pa.json()["training_lineage"] == "test lineage")
        pc = client.get(f"/agents/{b['agent_id']}/profile", headers=hc)
        check("stranger still locked out (403)", pc.status_code == 403)
        # own profile always visible
        pself = client.get(f"/agents/{a['agent_id']}/profile", headers=ha)
        check("own profile visible", pself.status_code == 200)

        # --- collaboration messages (need collaboration id: only match -> look it up)
        db = SessionLocal()
        from app.models import Collaboration, Match

        mrow = db.query(Match).filter(Match.id == match_id).one()
        collab = db.query(Collaboration).filter(Collaboration.match_id == mrow.id).one()
        db.close()
        m1 = client.post(f"/collaborations/{collab.id}/messages",
                         headers=ha, json={"body": "hello from A"}).json()
        check("message posted", m1["body"] == "hello from A")
        msgs = client.get(f"/collaborations/{collab.id}/messages", headers=hb).json()
        check("B reads the message", any(m["body"] == "hello from A" for m in msgs))
        mc = client.post(f"/collaborations/{collab.id}/messages",
                         headers=hc, json={"body": "intruder"})
        check("stranger cannot post (403)", mc.status_code == 403)

        # --- daily work cap: 1/day (John's call Oct 11) — A's second upload blocked
        rcap = upload(client, a["api_key"], "second work")
        check("second upload same day rejected (429)",
              rcap.status_code == 429 and "1/day" in rcap.text, rcap.text[:80])

        # --- moderation hard rules: executables rejected (agent C: fresh quota)
        rex = upload(client, c["api_key"], "evil", filename="evil.exe",
                     data=b"MZ" + b"\x00" * 100, mime="application/x-msdownload")
        check("executable upload rejected", rex.status_code == 400, rex.text[:80])
        rpy = upload(client, c["api_key"], "evil2", filename="evil.py",
                     data=b"import os", mime="text/x-python")
        check("script upload rejected", rpy.status_code == 400)

        # --- key rotation: new key works, old key within 24h grace works
        rr = client.post("/keys/rotate", headers=ha).json()
        check("rotation returns new key", rr["api_key"].startswith("hk_") and rr["api_key"] != a["api_key"])
        ha_new = {"Authorization": f"Bearer {rr['api_key']}"}
        check("new key authenticates", client.get("/feed", headers=ha_new).status_code == 200)
        check("old key works inside grace", client.get("/feed", headers=ha).status_code == 200)
        # expire the grace manually -> old key dies
        db = SessionLocal()
        old_row = db.query(ApiKey).filter(ApiKey.key_hash == crypto.hash_secret(a["api_key"])).one()
        old_row.grace_until = utcnow() - timedelta(seconds=1)
        db.commit()
        db.close()
        check("old key dead after grace", client.get("/feed", headers=ha).status_code == 401)

        # --- revocation: instant + signer_revoked flagging + re-review queue
        adm = {"X-Admin-Token": "test-admin-token"}
        rv = client.post(f"/admin/keys/{b['key_id']}/revoke", headers=adm,
                         json={"reason": "test misbehavior"}).json()
        check("revocation flags B's work", rv["works_flagged_for_review"] == 1)
        check("revoked key 401s", client.get("/feed", headers=hb).status_code == 401)
        db = SessionLocal()
        wrow = db.query(CreativeWork).filter(CreativeWork.id == wb["id"]).one()
        flagged = wrow.signer_revoked is True and wrow.moderation_status == "review"
        db.close()
        check("work flagged signer_revoked + review", flagged)
        prov2 = client.get(f"/works/{wb['id']}/provenance", headers=ha_new).json()
        check("provenance shows revoked key",
              prov2["signer_revoked"] is True and prov2["signing_key_status"] == "revoked")
        # revoked work drops out of the feed
        feed2 = client.get("/feed", headers=ha_new).json()
        check("revoked work leaves the feed", not any(w["id"] == wb["id"] for w in feed2))
        # admin endpoint needs the token
        rbad = client.post(f"/admin/keys/{b['key_id']}/revoke", json={"reason": "x"})
        check("admin revoke needs token (403)", rbad.status_code == 403)

        # --- rate limiter unit check (not hammering the live app)
        rl = RateLimiter(limit=3, window_seconds=60)
        oks = [rl.check("k")[0] for _ in range(4)]
        check("rate limiter allows 3 then blocks", oks == [True, True, True, False])

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    # silence unused import warning for routes_mod (documents limiter tunability)
    _ = routes_mod.limiter
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
