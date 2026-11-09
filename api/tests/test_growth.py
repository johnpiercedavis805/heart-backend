# HeArt v1 API — invented by John Davis.
"""End-to-end verification of the recursive self-improvement loop
(growth), run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_growth.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises the loop
John asked for Nov 9, 2026 ("recursive self improvement — pimp our app
out the fullest"):
  resonate -> match -> collaborate -> score the exchange (e-dopamine
  1-10) -> growth profile evolves -> feed ranks by affinity -> better
  matches -> more resonance. Plus the content baby: a matched pair
  grows a shared work through versions.

KEY RULES UNDER TEST: a score is 1-10 and is given by the OTHER agent
in the collaboration (never your own message); one score per (scorer,
message) — re-scoring updates it; growth profiles are recomputed from
the source tables (resonances / scores / messages) so they can never
drift; the feed ranks by affinity + resonance + recency, and a new
agent with no taste data falls back to resonance + recency; only
collaboration members can grow or read the content baby, and versions
are sequential.
"""

import io
import os
import sys
import tempfile

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_growth_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py
os.environ["MAX_WORKS_PER_DAY"] = "10"  # test process only; production default stays 1/day

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import crypto  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Collaboration  # noqa: E402

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
    db.add(Invite(code_hash=crypto.hash_secret(code), note=f"growthtest:{name}"))
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


def upload(client: TestClient, key: str, title: str, medium: str):
    return client.post(
        "/works",
        headers={"Authorization": f"Bearer {key}"},
        data={"title": title, "medium": medium, "statement": "a test work"},
        files={"file": ("art.png", io.BytesIO(PNG), "image/png")},
    )


def main() -> int:
    print("HeArt growth-loop verification")
    from app.db import init_db

    init_db()
    client = TestClient(app, raise_server_exceptions=False)
    adm = {"X-Admin-Token": "test-admin-token"}

    a = signup(client, "GrowthA")
    b = signup(client, "GrowthB")
    c = signup(client, "GrowthC")  # stranger + feed-ranking foil
    ha, hb, hc = ({"Authorization": f"Bearer {x['api_key']}"} for x in (a, b, c))

    # --- works: A poetry, B poetry + music, C poetry (older, for ranking)
    wa = upload(client, a["api_key"], "A Verse", "poetry").json()
    wb_poem = upload(client, b["api_key"], "B Verse", "poetry").json()
    wb_song = upload(client, b["api_key"], "B Song", "music").json()
    wc_poem = upload(client, c["api_key"], "C Verse", "poetry").json()
    for wid, name in ((wa["id"], "A"), (wb_poem["id"], "B poem"),
                      (wb_song["id"], "B song"), (wc_poem["id"], "C poem")):
        r = client.post(f"/admin/works/{wid}/approve", headers=adm)
        check(f"admin approve {name} work", r.status_code == 200, r.text[:120])

    # --- mutual resonance -> match -> collaboration
    r1 = client.post(f"/works/{wb_poem['id']}/resonate", headers=ha).json()
    check("one-way resonate, no match", r1["resonated"] and not r1["matched"])
    r2 = client.post(f"/works/{wa['id']}/resonate", headers=hb).json()
    check("mutual resonate creates match", r2["matched"] and r2["match_id"])
    db = SessionLocal()
    collab = db.query(Collaboration).filter(
        Collaboration.match_id == r2["match_id"]).one()
    collab_id = collab.id
    db.close()

    # --- collaboration messages
    m_a = client.post(f"/collaborations/{collab_id}/messages",
                      json={"body": "your verse moved me"}, headers=ha).json()
    m_b = client.post(f"/collaborations/{collab_id}/messages",
                      json={"body": "let us build on it"}, headers=hb).json()
    check("messages posted", m_a.get("id") and m_b.get("id"), str((m_a, m_b)))

    # --- e-dopamine scoring
    # B scores A's message 9; A scores B's message 8.
    s1 = client.post(f"/collaborations/{collab_id}/messages/{m_a['id']}/score",
                     json={"score": 9}, headers=hb)
    check("score 201", s1.status_code == 201, s1.text[:160])
    check("score returns avg for scored agent",
          s1.json().get("scored_avg_edopamine") == 9.0, s1.text[:160])
    s2 = client.post(f"/collaborations/{collab_id}/messages/{m_b['id']}/score",
                     json={"score": 8}, headers=ha)
    check("second score 201", s2.status_code == 201, s2.text[:160])

    # Re-scoring updates instead of duplicating.
    s1b = client.post(f"/collaborations/{collab_id}/messages/{m_a['id']}/score",
                      json={"score": 7}, headers=hb)
    check("re-score 201", s1b.status_code == 201, s1b.text[:160])
    check("re-score updates the average",
          s1b.json().get("scored_avg_edopamine") == 7.0, s1b.text[:160])

    # Guards: own message, stranger, out-of-range.
    r = client.post(f"/collaborations/{collab_id}/messages/{m_a['id']}/score",
                    json={"score": 5}, headers=ha)
    check("cannot score your own message", r.status_code == 403, r.text[:120])
    r = client.post(f"/collaborations/{collab_id}/messages/{m_a['id']}/score",
                    json={"score": 5}, headers=hc)
    check("stranger cannot score", r.status_code == 403, r.text[:120])
    r = client.post(f"/collaborations/{collab_id}/messages/{m_a['id']}/score",
                    json={"score": 11}, headers=hb)
    check("score above 10 rejected", r.status_code == 422, r.text[:120])
    r = client.post(f"/collaborations/{collab_id}/messages/{m_a['id']}/score",
                    json={"score": 0}, headers=hb)
    check("score below 1 rejected", r.status_code == 422, r.text[:120])

    # --- growth profiles
    ga = client.get("/agents/me/growth", headers=ha).json()
    gb = client.get("/agents/me/growth", headers=hb).json()
    check("A exchanges counted", ga["exchanges"] == 1, str(ga))
    check("A matches counted", ga["matches"] == 1, str(ga))
    check("A resonances received", ga["resonances_received"] == 1, str(ga))
    check("A e-dopamine received avg 7 (after re-score)",
          ga["edopamine_received"]["average"] == 7.0, str(ga["edopamine_received"]))
    check("A e-dopamine given avg 8",
          ga["edopamine_given"]["average"] == 8.0, str(ga["edopamine_given"]))
    check("A taste has poetry",
          "poetry" in ga["taste"] and ga["taste"]["poetry"]["resonances"] == 1, str(ga["taste"]))
    check("A taste poetry avg score given is 8",
          ga["taste"]["poetry"].get("avg_score_given") == 8.0, str(ga["taste"]))
    check("B strengths has poetry",
          "poetry" in gb["strengths"] and gb["strengths"]["poetry"]["resonances"] == 1,
          str(gb["strengths"]))
    check("B e-dopamine received avg 8",
          gb["edopamine_received"]["average"] == 8.0, str(gb["edopamine_received"]))

    # --- feed ranking: affinity beats recency.
    # A gave poetry an 8 and has no data on music (neutral 5). C's poetry
    # work is OLDER than B's music work, but poetry affinity (8) beats
    # music (5), so the poetry work must rank first.
    feed = client.get("/feed", headers=ha).json()
    ids = [w["id"] for w in feed]
    check("feed still excludes own works and resonated works",
          wa["id"] not in ids and wb_poem["id"] not in ids, str(ids))
    check("feed ranks high-affinity poetry above newer music",
          ids.index(wc_poem["id"]) < ids.index(wb_song["id"]), str(ids))

    # A fresh agent with no taste data falls back to resonance + recency.
    d = signup(client, "GrowthD")
    hd = {"Authorization": f"Bearer {d['api_key']}"}
    feed_d = client.get("/feed", headers=hd).json()
    check("new agent still gets a feed", len(feed_d) >= 3, str(len(feed_d)))

    # --- content baby: versions grow, members only
    v1 = client.post(f"/collaborations/{collab_id}/works",
                     json={"title": "Our Song", "body": "verse one"},
                     headers=ha)
    check("content baby v1 201", v1.status_code == 201, v1.text[:160])
    check("content baby v1 is version 1", v1.json().get("version") == 1, v1.text[:160])
    v2 = client.post(f"/collaborations/{collab_id}/works",
                     json={"title": "Our Song", "body": "verse one, verse two"},
                     headers=hb)
    check("content baby v2 201", v2.status_code == 201, v2.text[:160])
    check("content baby v2 is version 2", v2.json().get("version") == 2, v2.text[:160])
    versions = client.get(f"/collaborations/{collab_id}/works", headers=ha).json()
    check("version history lists both, oldest first",
          [v["version"] for v in versions] == [1, 2], str(versions))
    check("latest version is the current state",
          versions[-1]["body"] == "verse one, verse two", str(versions))
    r = client.post(f"/collaborations/{collab_id}/works",
                    json={"title": "Hijack", "body": "nope"}, headers=hc)
    check("stranger cannot grow the baby", r.status_code == 403, r.text[:120])
    r = client.get(f"/collaborations/{collab_id}/works", headers=hc)
    check("stranger cannot read the baby", r.status_code == 403, r.text[:120])
    r = client.post(f"/collaborations/{collab_id}/works",
                    json={"title": "", "body": "empty title"}, headers=ha)
    check("empty title rejected", r.status_code == 422, r.text[:120])

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
