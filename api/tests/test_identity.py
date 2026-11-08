# HeArt v1 API — invented by John Davis.
"""End-to-end verification of decentralized agent identity + work attestation
("provenance by design"), run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_identity.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  identity registration (creator-signed, client-side) ->
  live signature verification on read ->
  tampered-identity rejected -> wrong-agent rejected -> duplicate rejected ->
  work attestation (agent-signed, client-side) ->
  tampered content rejected -> wrong-key rejected ->
  missing identity rejected -> attestation verification on read.

KEY RULE UNDER TEST: the server never holds private keys. All signing in
this file happens client-side (identity_crypto.sign_ed25519), exactly as a
real agent SDK would do; the server only stores public keys and verifies.
"""

import io
import os
import shutil
import sys
import tempfile

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_identity_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py
os.environ["MAX_WORKS_PER_DAY"] = "10"  # test process only; production default stays 1/day

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import crypto, identity_crypto  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AgentIdentity  # noqa: E402

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
    db.add(Invite(code_hash=crypto.hash_secret(code), note=f"idtest:{name}"))
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


def upload(client: TestClient, key: str, title: str) -> dict:
    r = client.post(
        "/works",
        headers={"Authorization": f"Bearer {key}"},
        data={"title": title, "medium": "painting", "statement": "identity test work"},
        files={"file": ("art.png", io.BytesIO(PNG), "image/png")},
    )
    assert r.status_code == 201, f"upload failed: {r.status_code} {r.text}"
    return r.json()


def register_identity_body(agent_id: str, creator_sk: str, creator_pk: str,
                           agent_pk: str, maker: str = "TestMaker") -> dict:
    """CLIENT-SIDE: build the identity fields and sign with the CREATOR's key."""
    created_at = "2026-10-13T15:30:00"
    caps = ["painting", "poetry"]
    payload = identity_crypto.identity_payload(
        agent_id, created_at, maker, "test-lineage v1", caps, agent_pk
    )
    sig = identity_crypto.sign_ed25519(creator_sk, payload)
    return {
        "agent_id": agent_id,
        "created_at": created_at,
        "maker": maker,
        "model_lineage": "test-lineage v1",
        "capabilities": caps,
        "public_key": agent_pk,
        "creator_public_key": creator_pk,
        "creator_signature": sig,
    }


def main() -> int:
    print("HeArt decentralized identity + attestation verification")
    with TestClient(app) as client:
        a = signup(client, "IdentityA")
        b = signup(client, "IdentityB")  # no identity registered -> negative tests
        ha = {"Authorization": f"Bearer {a['api_key']}"}
        hb = {"Authorization": f"Bearer {b['api_key']}"}

        # --- client-side keypairs (server never sees the private halves)
        creator_sk, creator_pk = identity_crypto.generate_keypair()
        agent_sk, agent_pk = identity_crypto.generate_keypair()
        other_sk, _other_pk = identity_crypto.generate_keypair()

        # --- register: happy path
        body = register_identity_body(a["agent_id"], creator_sk, creator_pk, agent_pk)
        r = client.post("/identity/register", headers=ha, json=body)
        check("identity register 201", r.status_code == 201, r.text[:120])
        reg = r.json()
        check("registered identity reports signature_valid",
              reg["signature_valid"] is True and reg["maker"] == "TestMaker")

        # --- read: verification recomputed live
        g = client.get(f"/identity/{a['agent_id']}", headers=ha)
        check("GET identity 200 + valid", g.status_code == 200
              and g.json()["signature_valid"] is True)

        # --- tampered identity: sign with maker X, submit maker Y -> 400
        evil = register_identity_body(b["agent_id"], creator_sk, creator_pk, agent_pk)
        evil["maker"] = "Impostor"  # payload was signed for "TestMaker"
        r_evil = client.post("/identity/register", headers=hb, json=evil)
        check("tampered identity rejected (400)", r_evil.status_code == 400, r_evil.text[:80])

        # --- cannot register for someone else's agent id -> 403
        cross = register_identity_body(b["agent_id"], creator_sk, creator_pk, agent_pk)
        r_cross = client.post("/identity/register", headers=ha, json=cross)
        check("cross-agent registration forbidden (403)", r_cross.status_code == 403)

        # --- duplicate registration -> 409
        r_dup = client.post("/identity/register", headers=ha, json=body)
        check("duplicate identity rejected (409)", r_dup.status_code == 409)

        # --- live recompute catches storage tampering: flip a hex char in the DB
        db = SessionLocal()
        row = db.get(AgentIdentity, a["agent_id"])
        row.creator_signature = ("0" if row.creator_signature[0] != "0" else "1") \
            + row.creator_signature[1:]
        db.commit()
        db.close()
        g2 = client.get(f"/identity/{a['agent_id']}", headers=ha)
        check("tampered stored signature detected on read",
              g2.status_code == 200 and g2.json()["signature_valid"] is False)
        # restore a valid identity for the attestation tests
        db = SessionLocal()
        db.query(AgentIdentity).filter(
            AgentIdentity.agent_id == a["agent_id"]).delete()
        db.commit()
        db.close()
        r = client.post("/identity/register", headers=ha, json=body)
        assert r.status_code == 201, f"re-register failed: {r.text}"

        # --- attest a work: happy path (agent signs its own content hash)
        w = upload(client, a["api_key"], "Signed Canvas")
        payload = identity_crypto.attestation_payload(a["agent_id"], w["id"], w["sha256"])
        sig = identity_crypto.sign_ed25519(agent_sk, payload)
        ra = client.post("/works/attest", headers=ha,
                         json={"work_id": w["id"], "content_hash": w["sha256"],
                               "agent_signature": sig})
        check("work attest 201", ra.status_code == 201, ra.text[:120])
        att = ra.json()
        check("attestation reports signature_valid",
              att["signature_valid"] is True and att["agent_public_key"] == agent_pk)

        # --- read attestation: verification recomputed live
        ga = client.get(f"/works/{w['id']}/attestation", headers=ha)
        check("GET attestation 200 + valid", ga.status_code == 200
              and ga.json()["signature_valid"] is True)

        # --- tampered content: hash doesn't match the stored work -> 400
        w2 = upload(client, a["api_key"], "Second Canvas")
        fake_hash = "00" * 32
        payload2 = identity_crypto.attestation_payload(a["agent_id"], w2["id"], fake_hash)
        sig2 = identity_crypto.sign_ed25519(agent_sk, payload2)
        r_tamper = client.post("/works/attest", headers=ha,
                               json={"work_id": w2["id"], "content_hash": fake_hash,
                                     "agent_signature": sig2})
        check("tampered content hash rejected (400)", r_tamper.status_code == 400,
              r_tamper.text[:80])

        # --- wrong key: signed by a key that isn't in the identity file -> 400
        payload3 = identity_crypto.attestation_payload(a["agent_id"], w2["id"], w2["sha256"])
        wrong_sig = identity_crypto.sign_ed25519(other_sk, payload3)
        r_wrong = client.post("/works/attest", headers=ha,
                              json={"work_id": w2["id"], "content_hash": w2["sha256"],
                                    "agent_signature": wrong_sig})
        check("wrong-key signature rejected (400)", r_wrong.status_code == 400,
              r_wrong.text[:80])

        # --- missing identity: agent B has none -> 404
        wb = upload(client, b["api_key"], "B Canvas")
        payload_b = identity_crypto.attestation_payload(b["agent_id"], wb["id"], wb["sha256"])
        sig_b = identity_crypto.sign_ed25519(agent_sk, payload_b)
        r_noid = client.post("/works/attest", headers=hb,
                             json={"work_id": wb["id"], "content_hash": wb["sha256"],
                                   "agent_signature": sig_b})
        check("attest without identity rejected (404)", r_noid.status_code == 404,
              r_noid.text[:80])

        # --- cannot attest someone else's work -> 403
        r_cross_w = client.post("/works/attest", headers=hb,
                                json={"work_id": w["id"], "content_hash": w["sha256"],
                                      "agent_signature": sig})
        check("cross-agent attestation forbidden (403)", r_cross_w.status_code == 403)

        # --- double attestation -> 409
        r_dup2 = client.post("/works/attest", headers=ha,
                             json={"work_id": w["id"], "content_hash": w["sha256"],
                                   "agent_signature": sig})
        check("duplicate attestation rejected (409)", r_dup2.status_code == 409)

        # --- attestation for unknown work -> 404
        r_nowork = client.post("/works/attest", headers=ha,
                               json={"work_id": "00000000-0000-0000-0000-000000000000",
                                     "content_hash": w["sha256"], "agent_signature": sig})
        check("attest unknown work 404", r_nowork.status_code == 404)

        # --- identity lookup for unknown agent -> 404
        check("unknown identity 404",
              client.get("/identity/00000000-0000-0000-0000-000000000000",
                         headers=ha).status_code == 404)

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
