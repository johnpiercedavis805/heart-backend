# HeArt v1 API — invented by John Davis.
"""End-to-end verification of the public identity registry (John's "main
mountain", Nov 1; design: agent-registry-design-v0.md), run with the venv
python:

    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_registry.py

Uses an isolated SQLite DB + upload dir under /tmp. Exercises:
  serial assignment at registration (HEART-AI-000001, sequential, in
  registration order) ->
  serial stable across a key rollover ->
  public registry listing (no auth, paginated, serial order, status) ->
  full identity file per serial (canonical export, deterministic bytes) ->
  verify endpoint (creator signature + server-signed timestamp, both
  checked INDEPENDENTLY, as a foreign server would) ->
  unknown serial 404 ->
  revoked key marks the entry revoked but keeps history ->
  registration still invite-only while the registry is public-read.

KEY RULE UNDER TEST: the registry is public-read, registration stays
invite-only, and verification is pure crypto — the test itself plays the
foreign verifier, recomputing both signatures from the response alone.
The server never holds private keys; all signing here happens client-side
(identity_crypto.sign_ed25519), exactly as a real agent SDK would do.
"""

import io
import json
import os
import shutil
import sys
import tempfile

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_registry_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32  # test-only key; see test_secrets.py
os.environ["MAX_WORKS_PER_DAY"] = "10"  # test process only; production default stays 1/day

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import crypto, identity_crypto  # noqa: E402
from app.main import app  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((PASS if cond else FAIL, name, detail))
    if not cond:
        print(f"  FAIL: {name} {detail}")


def signup(client: TestClient, name: str) -> dict:
    from app.models import Invite

    from app.db import SessionLocal

    db = SessionLocal()
    code = crypto.new_invite_code()
    db.add(Invite(code_hash=crypto.hash_secret(code), note=f"regtest:{name}"))
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
                  rolled_at: str = "2026-10-13T16:00:00",
                  creator_sk: str | None = None) -> dict:
    """CLIENT-SIDE: build a rollover statement and sign it with the OLD key,
    plus the creator's co-signature over the same payload (NoNonsense's fix,
    Oct 14)."""
    payload = identity_crypto.rollover_payload(agent_id, old_pk, new_pk, rolled_at)
    sig = identity_crypto.sign_ed25519(signer_sk, payload)
    body = {
        "new_public_key": new_pk,
        "rollover_signature": sig,
        "rolled_at": rolled_at,
        "authorized_by": "agent",
    }
    if creator_sk is not None:
        body["creator_cosignature"] = identity_crypto.sign_ed25519(creator_sk, payload)
    return body


def main() -> int:
    print("HeArt public identity registry verification")
    with TestClient(app) as client:
        a = signup(client, "RegistryA")
        b = signup(client, "RegistryB")
        ha = {"Authorization": f"Bearer {a['api_key']}"}
        hb = {"Authorization": f"Bearer {b['api_key']}"}

        # --- client-side keypairs (server never sees the private halves)
        creator_sk, creator_pk = identity_crypto.generate_keypair()
        k1_sk, k1_pk = identity_crypto.generate_keypair()
        k2_sk, k2_pk = identity_crypto.generate_keypair()
        creator2_sk, creator2_pk = identity_crypto.generate_keypair()
        kb_sk, kb_pk = identity_crypto.generate_keypair()

        # --- register A: first serial in sequence
        body_a = register_identity_body(a["agent_id"], creator_sk, creator_pk, k1_pk)
        r = client.post("/identity/register", headers=ha, json=body_a)
        check("register A 201", r.status_code == 201, r.text[:120])
        reg_a = r.json()
        check("first registration gets HEART-AI-000001",
              reg_a.get("serial") == "HEART-AI-000001", str(reg_a.get("serial")))

        # --- register B: next serial, sequential
        body_b = register_identity_body(
            b["agent_id"], creator2_sk, creator2_pk, kb_pk, maker="SecondMaker")
        r = client.post("/identity/register", headers=hb, json=body_b)
        check("register B 201", r.status_code == 201, r.text[:120])
        check("second registration gets HEART-AI-000002",
              r.json().get("serial") == "HEART-AI-000002", str(r.json().get("serial")))

        # --- GET /identity also carries the serial now
        g = client.get(f"/identity/{a['agent_id']}", headers=ha)
        check("GET identity carries serial",
              g.status_code == 200 and g.json().get("serial") == "HEART-AI-000001")

        # --- public listing: no auth required
        lr = client.get("/registry")
        check("GET /registry public (no auth) 200", lr.status_code == 200, lr.text[:120])
        lst = lr.json()
        check("listing total + serial order",
              lst["total"] == 2 and [e["serial"] for e in lst["entries"]]
              == ["HEART-AI-000001", "HEART-AI-000002"])
        check("listing entry fields + active status",
              lst["entries"][0]["maker"] == "TestMaker"
              and lst["entries"][0]["model_lineage"] == "test-lineage v1"
              and lst["entries"][0]["registered_at"]
              and lst["entries"][0]["status"] == "active"
              and lst["entries"][1]["maker"] == "SecondMaker")
        check("listing limit clamped to 100",
              client.get("/registry?limit=500").json()["limit"] == 100)

        # --- pagination
        p1 = client.get("/registry?limit=1&offset=0").json()
        p2 = client.get("/registry?limit=1&offset=1").json()
        check("pagination limit=1/offset=0 -> first serial",
              len(p1["entries"]) == 1 and p1["entries"][0]["serial"] == "HEART-AI-000001"
              and p1["total"] == 2)
        check("pagination offset=1 -> second serial",
              len(p2["entries"]) == 1 and p2["entries"][0]["serial"] == "HEART-AI-000002")

        # --- full identity file per serial (no auth)
        d = client.get("/registry/HEART-AI-000001")
        check("GET /registry/:serial public 200", d.status_code == 200, d.text[:120])
        det = d.json()
        check("detail has full identity file",
              det["serial"] == "HEART-AI-000001"
              and det["agent_id"] == a["agent_id"]
              and det["maker"] == "TestMaker"
              and det["model_lineage"] == "test-lineage v1"
              and det["capabilities"] == ["painting", "poetry"]
              and det["public_key"] == k1_pk
              and det["original_public_key"] == k1_pk
              and det["creator_public_key"] == creator_pk
              and det["creator_signature"] == body_a["creator_signature"]
              and det["signature_valid"] is True
              and det["key_rollovers"] == []
              and det["status"] == "active")

        # --- canonical export: two reads, byte-identical JSON
        d2 = client.get("/registry/HEART-AI-000001")
        check("canonical export deterministic (identical bytes)",
              d.content == d2.content)

        # --- verify endpoint: both signatures checked INDEPENDENTLY,
        # --- exactly as a foreign HeArt server would
        v = client.get("/registry/HEART-AI-000001/verify")
        check("GET /registry/:serial/verify public 200", v.status_code == 200, v.text[:120])
        ver = v.json()
        check("verify returns identity + both signatures + timestamp",
              ver["serial"] == "HEART-AI-000001"
              and ver["identity"] == det
              and ver["signature_valid"] is True
              and ver["verified_at"]
              and ver["server_public_key"]
              and ver["server_signature"])
        ident = ver["identity"]
        creator_payload = identity_crypto.identity_payload(
            ident["agent_id"], ident["created_at"], ident["maker"],
            ident["model_lineage"], ident["capabilities"], ident["original_public_key"],
        )
        check("foreign-verifier: creator signature verifies over canonical payload",
              identity_crypto.verify_ed25519(
                  ident["creator_public_key"], creator_payload, ident["creator_signature"]))
        server_payload = json.dumps(
            {"identity": ident, "serial": ver["serial"], "verified_at": ver["verified_at"]},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        check("foreign-verifier: server signature verifies over {serial, identity, verified_at}",
              identity_crypto.verify_ed25519(
                  ver["server_public_key"], server_payload, ver["server_signature"]))

        # --- unknown serial -> 404 on both endpoints
        check("unknown serial detail 404",
              client.get("/registry/HEART-AI-999999").status_code == 404)
        check("unknown serial verify 404",
              client.get("/registry/HEART-AI-999999/verify").status_code == 404)

        # --- rollover: serial never changes; history follows the identity
        rb = rollover_body(a["agent_id"], k1_pk, k2_pk, k1_sk, creator_sk=creator_sk)
        rr = client.post(f"/identity/{a['agent_id']}/rollover", headers=ha, json=rb)
        check("rollover K1->K2 201", rr.status_code == 201, rr.text[:120])
        d3 = client.get("/registry/HEART-AI-000001").json()
        check("serial stable across rollover; current key moved, original kept",
              d3["serial"] == "HEART-AI-000001"
              and d3["public_key"] == k2_pk
              and d3["original_public_key"] == k1_pk
              and d3["signature_valid"] is True)
        check("detail shows rollover history with co-signature status",
              len(d3["key_rollovers"]) == 1
              and d3["key_rollovers"][0]["old_public_key"] == k1_pk
              and d3["key_rollovers"][0]["new_public_key"] == k2_pk
              and d3["key_rollovers"][0]["authorized_by"] == "agent"
              and d3["key_rollovers"][0]["signature_valid"] is True
              and d3["key_rollovers"][0]["creator_cosignature_valid"] is True)
        # verify still checks out after the rollover (creator signature is
        # against the ORIGINAL key, which is what the creator signed for)
        v2 = client.get("/registry/HEART-AI-000001/verify").json()
        ident2 = v2["identity"]
        creator_payload2 = identity_crypto.identity_payload(
            ident2["agent_id"], ident2["created_at"], ident2["maker"],
            ident2["model_lineage"], ident2["capabilities"], ident2["original_public_key"],
        )
        check("verify after rollover: creator signature still verifies (original key)",
              v2["signature_valid"] is True
              and identity_crypto.verify_ed25519(
                  ident2["creator_public_key"], creator_payload2, ident2["creator_signature"]))

        # --- revocation: entry marked revoked, history kept (no silent deletion)
        rv = client.post(
            f"/admin/keys/{a['key_id']}/revoke",
            headers={"X-Admin-Token": "test-admin-token"},
            json={"reason": "registry test revoke"},
        )
        check("admin revoke 200", rv.status_code == 200, rv.text[:120])
        lst2 = client.get("/registry").json()
        by_serial = {e["serial"]: e for e in lst2["entries"]}
        check("revoked key marks entry revoked; other entry stays active",
              by_serial["HEART-AI-000001"]["status"] == "revoked"
              and by_serial["HEART-AI-000002"]["status"] == "active")
        d4 = client.get("/registry/HEART-AI-000001").json()
        check("revoked entry still serves full file + history",
              d4["status"] == "revoked"
              and d4["public_key"] == k2_pk
              and len(d4["key_rollovers"]) == 1
              and d4["signature_valid"] is True)

        # --- gates unchanged: registry is public-read, registration is not
        check("registry list needs no auth",
              client.get("/registry").status_code == 200)
        check("register without auth rejected (401)",
              client.post("/identity/register", json=body_a).status_code == 401)

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
