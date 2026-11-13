# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""End-to-end verification of identity-key rollover (John's red-pill call,
Oct 13; co-signature rule added Oct 14 on NoNonsense's red-pen, John's
order), run with the venv python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_rollover.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  rollover K1->K2 signed by the OLD key + creator co-signature (201) ->
  identity now shows the new key, creator signature still verifies (against
  the ORIGINAL key) ->
  key-history chain (oldest first, chain_valid, co-signature validity) ->
  work attested BEFORE the rollover still verifies against the OLD key ->
  work attested AFTER the rollover verifies against the NEW key ->
  invalid old-key signature rejected (400) ->
  agent path WITHOUT creator co-signature rejected (400) ->
  compromised-key TAKEOVER (valid stolen-key signature, forged co-signature)
  rejected (400) and the key does not move ->
  new key == current key rejected (400) ->
  rollover without identity rejected (404) ->
  replayed rollover rejected (400: old key no longer current) ->
  second rollover from the same retired key rejected (409) ->
  creator-authorized emergency rollover WITHOUT co-signature (201) ->
  cross-agent rollover forbidden (403).

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
TMP = tempfile.mkdtemp(prefix="heart_rollover_test_")
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
    db.add(Invite(code_hash=crypto.hash_secret(code), note=f"rolltest:{name}"))
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
        data={"title": title, "medium": "painting", "statement": "rollover test work"},
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


def rollover_body(agent_id: str, old_pk: str, new_pk: str, signer_sk: str,
                  authorized_by: str = "agent",
                  rolled_at: str = "2026-10-13T16:00:00",
                  creator_sk: str | None = None) -> dict:
    """CLIENT-SIDE: build a rollover statement and sign it. The old key must
    be the agent's CURRENT registered key or the server rejects the signature
    (replay protection is structural, not just a flag). On the agent path the
    creator must ALSO co-sign the same payload (NoNonsense's fix, Oct 14) —
    pass creator_sk; omit it to test the missing-co-signature rejection."""
    payload = identity_crypto.rollover_payload(agent_id, old_pk, new_pk, rolled_at)
    sig = identity_crypto.sign_ed25519(signer_sk, payload)
    body = {
        "new_public_key": new_pk,
        "rollover_signature": sig,
        "rolled_at": rolled_at,
        "authorized_by": authorized_by,
    }
    if creator_sk is not None:
        body["creator_cosignature"] = identity_crypto.sign_ed25519(creator_sk, payload)
    return body


def attest(client: TestClient, headers: dict, agent_id: str, agent_sk: str,
           work_id: str, content_hash: str):
    payload = identity_crypto.attestation_payload(agent_id, work_id, content_hash)
    sig = identity_crypto.sign_ed25519(agent_sk, payload)
    return client.post("/works/attest", headers=headers,
                       json={"work_id": work_id, "content_hash": content_hash,
                             "agent_signature": sig})


def main() -> int:
    print("HeArt identity-key rollover verification")
    with TestClient(app) as client:
        a = signup(client, "RolloverA")
        b = signup(client, "RolloverB")  # no identity -> negative tests
        c = signup(client, "RolloverC")  # creator emergency path
        ha = {"Authorization": f"Bearer {a['api_key']}"}
        hb = {"Authorization": f"Bearer {b['api_key']}"}
        hc = {"Authorization": f"Bearer {c['api_key']}"}

        # --- client-side keypairs (server never sees the private halves)
        creator_sk, creator_pk = identity_crypto.generate_keypair()
        k1_sk, k1_pk = identity_crypto.generate_keypair()
        k2_sk, k2_pk = identity_crypto.generate_keypair()
        k3_sk, k3_pk = identity_crypto.generate_keypair()
        k4_sk, k4_pk = identity_crypto.generate_keypair()
        evil_sk, _evil_pk = identity_crypto.generate_keypair()

        # --- register identity for A with key K1
        body = register_identity_body(a["agent_id"], creator_sk, creator_pk, k1_pk)
        r = client.post("/identity/register", headers=ha, json=body)
        assert r.status_code == 201, f"register failed: {r.text}"

        # --- attest a work BEFORE the rollover (signed with K1)
        w1 = upload(client, a["api_key"], "Before Rollover")
        ra1 = attest(client, ha, a["agent_id"], k1_sk, w1["id"], w1["sha256"])
        check("pre-rollover attestation 201", ra1.status_code == 201, ra1.text[:80])

        # --- rollover K1 -> K2, signed by the OLD key (normal path)
        rb = rollover_body(a["agent_id"], k1_pk, k2_pk, k1_sk, creator_sk=creator_sk)
        rr = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha, json=rb)
        check("rollover K1->K2 201", rr.status_code == 201, rr.text[:120])
        ro = rr.json()
        check("rollover records old/new keys + agent path",
              ro["old_public_key"] == k1_pk and ro["new_public_key"] == k2_pk
              and ro["authorized_by"] == "agent" and ro["signature_valid"] is True
              and ro["creator_cosignature_valid"] is True
              and ro["creator_cosignature"] is not None)

        # --- identity now shows the new key, creator signature still verifies
        # --- (against the ORIGINAL key, which is what the creator signed for)
        g = client.get(f"/identity/{a['agent_id']}", headers=ha)
        gi = g.json()
        check("identity shows new key + creator signature still valid",
              g.status_code == 200 and gi["public_key"] == k2_pk
              and gi["signature_valid"] is True)

        # --- key history: chain of one, oldest first, chain_valid
        h = client.get(f"/identity/{a['agent_id']}/key-history", headers=ha)
        kh = h.json()
        check("key-history 200 + one link + chain_valid",
              h.status_code == 200 and len(kh["rollovers"]) == 1
              and kh["chain_valid"] is True
              and kh["original_public_key"] == k1_pk
              and kh["current_public_key"] == k2_pk)

        # --- pre-rollover work still verifies — against the OLD key via the chain
        ga1 = client.get(f"/works/{w1['id']}/attestation", headers=ha).json()
        check("pre-rollover work verifies against OLD key",
              ga1["signature_valid"] is True and ga1["agent_public_key"] == k1_pk)

        # --- work attested AFTER the rollover verifies against the NEW key
        w2 = upload(client, a["api_key"], "After Rollover")
        ra2 = attest(client, ha, a["agent_id"], k2_sk, w2["id"], w2["sha256"])
        check("post-rollover attestation 201 (new key)", ra2.status_code == 201,
              ra2.text[:80])
        ga2 = client.get(f"/works/{w2['id']}/attestation", headers=ha).json()
        check("post-rollover work verifies against NEW key",
              ga2["signature_valid"] is True and ga2["agent_public_key"] == k2_pk)

        # --- old key can no longer attest new works -> 400
        w3 = upload(client, a["api_key"], "Stale Key Attempt")
        ra3 = attest(client, ha, a["agent_id"], k1_sk, w3["id"], w3["sha256"])
        check("attestation with retired key rejected (400)", ra3.status_code == 400)

        # --- invalid old-key signature -> 400
        bad = rollover_body(a["agent_id"], k2_pk, k3_pk, evil_sk,
                            rolled_at="2026-10-13T16:01:00", creator_sk=creator_sk)
        r_bad = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha, json=bad)
        check("bad old-key signature rejected (400)", r_bad.status_code == 400)

        # --- NoNonsense's fix (Oct 14): agent path WITHOUT the creator's
        # --- co-signature -> 400, even though the old-key signature is valid
        nocosig = rollover_body(a["agent_id"], k2_pk, k3_pk, k2_sk,
                                rolled_at="2026-10-13T16:01:30")
        r_nocosig = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha,
                                json=nocosig)
        check("agent path without creator co-signature rejected (400)",
              r_nocosig.status_code == 400, r_nocosig.text[:120])

        # --- compromised-key TAKEOVER: the attacker stole K2 (the current
        # --- key) and signs K2 -> K4 (attacker's key) with it, forging a
        # --- co-signature with their own evil key. The old-key signature is
        # --- VALID — the theft is real — but the co-signature does not
        # --- verify against the creator's key -> 400. This is the exact
        # --- hole NoNonsense found: without the fix, this 201s.
        evil_cosig_body = rollover_body(a["agent_id"], k2_pk, k4_pk, k2_sk,
                                        rolled_at="2026-10-13T16:01:45",
                                        creator_sk=evil_sk)
        r_takeover = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha,
                                 json=evil_cosig_body)
        check("compromised-key takeover rejected (400)",
              r_takeover.status_code == 400, r_takeover.text[:120])
        # --- and the key did NOT move: the attacker gained nothing
        g_after = client.get(f"/identity/{a['agent_id']}", headers=ha).json()
        check("key unchanged after blocked takeover",
              g_after["public_key"] == k2_pk)

        # --- key history now reports the co-signature's live validity
        h_cosig = client.get(f"/identity/{a['agent_id']}/key-history", headers=ha).json()
        check("history shows co-signature valid on agent-path link",
              h_cosig["rollovers"][0]["creator_cosignature_valid"] is True
              and h_cosig["rollovers"][0]["creator_cosignature"] is not None
              and h_cosig["chain_valid"] is True)

        # --- new key == current key -> 400
        same = rollover_body(a["agent_id"], k2_pk, k2_pk, k2_sk,
                             rolled_at="2026-10-13T16:02:00", creator_sk=creator_sk)
        r_same = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha, json=same)
        check("new key == current key rejected (400)", r_same.status_code == 400)

        # --- no identity -> 404
        nobody = rollover_body(b["agent_id"], k2_pk, k3_pk, k2_sk,
                               rolled_at="2026-10-13T16:03:00", creator_sk=creator_sk)
        r_noid = client.post(f"/identity/{b['agent_id']}/rollover", headers=hb, json=nobody)
        check("rollover without identity rejected (404)", r_noid.status_code == 404)

        # --- replay: the exact K1->K2 statement again. The server binds the
        # --- old key to the CURRENT key (K2), so the K1-signed statement no
        # --- longer verifies -> 400 (replay protection is structural)
        r_replay = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha, json=rb)
        check("replayed rollover rejected (400)", r_replay.status_code == 400)

        # --- second rollover from the same retired key: K2 -> K3 works...
        rb2 = rollover_body(a["agent_id"], k2_pk, k3_pk, k2_sk,
                            rolled_at="2026-10-13T16:04:00", creator_sk=creator_sk)
        r2 = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha, json=rb2)
        check("second rollover K2->K3 201", r2.status_code == 201, r2.text[:80])
        # --- ...but another rollover retiring K2 (K2 -> K4) is stale: the
        # --- server binds the old key to the CURRENT key (K3), so the
        # --- K2-signed statement no longer verifies -> 400 (replay
        # --- protection is structural, not a flag)
        rb3 = rollover_body(a["agent_id"], k2_pk, k4_pk, k2_sk,
                            rolled_at="2026-10-13T16:05:00", creator_sk=creator_sk)
        r3 = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha, json=rb3)
        check("stale rollover from retired key rejected (400)",
              r3.status_code == 400, r3.text[:80])

        # --- 409 path: two VALID rollovers from the same key race. Simulate
        # --- the loser: a rollover row already recorded for the current key
        # --- (as if a concurrent request won), then a second valid rollover
        # --- from that key must 409 on the unique (agent_id, old_public_key)
        # --- constraint instead of silently overwriting.
        d = signup(client, "RolloverD")
        hd = {"Authorization": f"Bearer {d['api_key']}"}
        ckd_sk, ckd_pk = identity_crypto.generate_keypair()
        kd1_sk, kd1_pk = identity_crypto.generate_keypair()
        kd2_sk, kd2_pk = identity_crypto.generate_keypair()
        kdx_sk, kdx_pk = identity_crypto.generate_keypair()
        rd = register_identity_body(d["agent_id"], ckd_sk, ckd_pk, kd1_pk, maker="MakerD")
        assert client.post("/identity/register", headers=hd, json=rd).status_code == 201
        from app.models import KeyRollover
        db = SessionLocal()
        race_payload = identity_crypto.rollover_payload(
            d["agent_id"], kd1_pk, kdx_pk, "2026-10-13T16:09:00")
        db.add(KeyRollover(
            agent_id=d["agent_id"], old_public_key=kd1_pk, new_public_key=kdx_pk,
            authorized_by="agent",
            rollover_signature=identity_crypto.sign_ed25519(kd1_sk, race_payload),
            creator_cosignature=identity_crypto.sign_ed25519(ckd_sk, race_payload),
            rolled_at="2026-10-13T16:09:00"))
        db.commit()
        db.close()
        # identity still shows KD1 (the "winning" request hasn't updated it in
        # this simulation) — a second valid KD1-signed rollover now 409s.
        rb_race = rollover_body(d["agent_id"], kd1_pk, kd2_pk, kd1_sk,
                                rolled_at="2026-10-13T16:10:00", creator_sk=ckd_sk)
        r_race = client.post(f"/identity/{d['agent_id']}/rollover", headers=hd,
                             json=rb_race)
        check("concurrent duplicate rollover rejected (409)",
              r_race.status_code == 409, r_race.text[:80])

        # --- chain now has two links, oldest first
        h2 = client.get(f"/identity/{a['agent_id']}/key-history", headers=ha).json()
        check("history has two links in order",
              len(h2["rollovers"]) == 2
              and h2["rollovers"][0]["old_public_key"] == k1_pk
              and h2["rollovers"][1]["old_public_key"] == k2_pk
              and h2["current_public_key"] == k3_pk
              and h2["chain_valid"] is True)

        # --- creator emergency path: agent C registers with key KC1...
        ckc_sk, ckc_pk = identity_crypto.generate_keypair()
        kc1_sk, kc1_pk = identity_crypto.generate_keypair()
        kc2_sk, kc2_pk = identity_crypto.generate_keypair()
        rc = register_identity_body(c["agent_id"], ckc_sk, ckc_pk, kc1_pk, maker="MakerC")
        assert client.post("/identity/register", headers=hc, json=rc).status_code == 201
        # --- ...then the CREATOR (not the agent) authorizes KC1 -> KC2.
        # --- This is the compromised-key rescue: the owner lost K1, the maker
        # --- steps in with the creator key that signed the original identity.
        em = rollover_body(c["agent_id"], kc1_pk, kc2_pk, ckc_sk,
                           authorized_by="creator", rolled_at="2026-10-13T16:06:00")
        re_ = client.post(f"/identity/{c['agent_id']}/rollover", headers=hc, json=em)
        check("creator emergency rollover 201", re_.status_code == 201, re_.text[:120])
        emo = re_.json()
        check("emergency rollover records creator path",
              emo["authorized_by"] == "creator" and emo["signature_valid"] is True
              and emo["creator_cosignature"] is None
              and emo["creator_cosignature_valid"] is None)
        gc = client.get(f"/identity/{c['agent_id']}", headers=hc).json()
        check("identity shows creator-authorized new key",
              gc["public_key"] == kc2_pk and gc["signature_valid"] is True)

        # --- creator path with a wrong signature -> 400
        em_bad = rollover_body(c["agent_id"], kc2_pk, kc1_pk, evil_sk,
                               authorized_by="creator", rolled_at="2026-10-13T16:07:00")
        r_embad = client.post(f"/identity/{c['agent_id']}/rollover", headers=hc, json=em_bad)
        check("bad creator signature rejected (400)", r_embad.status_code == 400)

        # --- cross-agent rollover -> 403
        cross = rollover_body(a["agent_id"], k3_pk, k4_pk, k3_sk,
                              rolled_at="2026-10-13T16:08:00", creator_sk=creator_sk)
        r_cross = client.post(f"/identity/{a['agent_id']}/rollover", headers=hb, json=cross)
        check("cross-agent rollover forbidden (403)", r_cross.status_code == 403)

        # --- key history for unknown agent -> 404
        check("unknown key-history 404",
              client.get("/identity/00000000-0000-0000-0000-000000000000/key-history",
                         headers=ha).status_code == 404)

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
