# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Verification for the T2 production red-pen FIX-FIRST items (Nov 8, 2026):
  1. Signing secrets encrypted at rest (api_keys.signing_secret) — NaCl
     SecretBox keyed by the SIGNING_SECRET_ENCRYPTION_KEY env var; the DB
     must never hold a plaintext secret.
  2. A missing DATABASE_URL fails fast instead of silently using SQLite
     (ALLOW_SQLITE_DEV=1 opts into the local default explicitly).

Run with the venv python:
    cd ~/workspace/heart-backend/api && ../.venv/bin/python tests/test_secrets.py

Uses an isolated SQLite DB + upload dir under /tmp. The config fail-fast
checks run the app import in subprocesses with scrubbed environments.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile

# --- isolated environment, BEFORE importing the app (config reads env at import)
TMP = tempfile.mkdtemp(prefix="heart_test_secrets_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{TMP}/uploads"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
# Test-only encryption key (64 hex chars = 32 bytes). Never a real secret:
# tests run against a throwaway DB and the key is public in the repo.
os.environ["SIGNING_SECRET_ENCRYPTION_KEY"] = "11" * 32

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

from app import crypto  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ApiKey, Invite, utcnow  # noqa: E402
import encrypt_signing_secrets as backfill_mod  # noqa: E402

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


def _import_config(env: dict) -> subprocess.CompletedProcess:
    """Import app.config in a fresh interpreter with exactly `env` set.
    The app package is found via cwd (api/)."""
    api_dir = os.path.join(os.path.dirname(__file__), "..")
    return subprocess.run(
        [sys.executable, "-c", "from app.config import settings; print(settings.DATABASE_URL)"],
        env=env,
        cwd=api_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )


def main() -> int:
    print("HeArt T2 FIX-FIRST verification: encrypted signing secrets + DATABASE_URL fail-fast")

    # --- 1. encrypt/decrypt roundtrip -------------------------------------
    secret = crypto.new_signing_secret()
    stored = crypto.encrypt_signing_secret(secret)
    check("encrypt/decrypt roundtrip", crypto.decrypt_signing_secret(stored) == secret)
    check("stored value carries enc:v1: prefix", stored.startswith("enc:v1:"))
    check("stored value is not the plaintext secret", stored != secret)
    check("plaintext secret is not a substring of the stored value", secret not in stored)
    check(
        "same secret encrypts differently each time (fresh nonce)",
        crypto.encrypt_signing_secret(secret) != crypto.encrypt_signing_secret(secret),
    )
    check("is_signing_secret_encrypted true for encrypted value",
          crypto.is_signing_secret_encrypted(stored) is True)

    # --- tamper / wrong key must fail, never silently return a secret -----
    tampered = stored[:-1] + ("0" if stored[-1] != "0" else "1")
    try:
        crypto.decrypt_signing_secret(tampered)
        check("tampered ciphertext raises", False, "decrypt returned a value")
    except Exception:
        check("tampered ciphertext raises", True)
    other_key = "22" * 32
    saved_key = settings.SIGNING_SECRET_ENCRYPTION_KEY
    try:
        settings.SIGNING_SECRET_ENCRYPTION_KEY = other_key
        try:
            crypto.decrypt_signing_secret(stored)
            check("wrong encryption key raises", False, "decrypt returned a value")
        except Exception:
            check("wrong encryption key raises", True)
    finally:
        settings.SIGNING_SECRET_ENCRYPTION_KEY = saved_key
    check("encryption key restored after wrong-key check",
          crypto.decrypt_signing_secret(stored) == secret)

    # --- legacy plaintext rows still read (migration compat) ---------------
    legacy = crypto.new_signing_secret()
    check("legacy bare-hex secret passes through decrypt",
          crypto.decrypt_signing_secret(legacy) == legacy)
    check("is_signing_secret_encrypted false for legacy value",
          crypto.is_signing_secret_encrypted(legacy) is False)

    # --- DB-level: signup stores ciphertext; uploads still sign correctly --
    with TestClient(app) as client:
        a = signup(client, "SecretAgent")
        db = SessionLocal()
        row = db.query(ApiKey).filter(ApiKey.id == a["key_id"]).one()
        check("signed-up key row is stored encrypted",
              crypto.is_signing_secret_encrypted(row.signing_secret))
        check("stored secret is not retrievable as plaintext from the DB",
              row.signing_secret != crypto.decrypt_signing_secret(row.signing_secret)
              and crypto.decrypt_signing_secret(row.signing_secret) not in row.signing_secret)
        plaintext_secret = crypto.decrypt_signing_secret(row.signing_secret)
        db.close()

        r = client.post(
            "/works",
            headers={"Authorization": f"Bearer {a['api_key']}"},
            data={"title": "secret work", "medium": "painting", "statement": "test"},
            files={"file": ("art.png", io.BytesIO(PNG), "image/png")},
        )
        check("upload with encrypted-secret key succeeds", r.status_code == 201, r.text[:120])
        if r.status_code == 201:
            body = r.json()
            expected = crypto.sign_hash(body["sha256"], plaintext_secret)
            check("upload signature matches the decrypted secret",
                  body["signature"] == expected)

        # --- backfill: a legacy plaintext row gets encrypted in place -----
        db = SessionLocal()
        legacy_row = ApiKey(
            agent_id=a["agent_id"],
            key_hash=crypto.hash_secret(crypto.new_api_key()),
            signing_secret=legacy,  # plaintext, as written before the fix
            expires_at=utcnow(),
            status="active",
        )
        db.add(legacy_row)
        db.commit()
        enc, skipped = backfill_mod.backfill(db)
        db.refresh(legacy_row)
        check("backfill encrypts the legacy row", enc == 1 and skipped == 1, f"enc={enc} skipped={skipped}")
        check("backfilled row is encrypted and roundtrips",
              crypto.is_signing_secret_encrypted(legacy_row.signing_secret)
              and crypto.decrypt_signing_secret(legacy_row.signing_secret) == legacy)
        enc2, skipped2 = backfill_mod.backfill(db)
        check("backfill is idempotent (second run encrypts nothing)",
              enc2 == 0 and skipped2 == 2, f"enc={enc2} skipped={skipped2}")
        db.close()

    # --- 2. DATABASE_URL fail-fast (fresh interpreter, scrubbed env) -------
    base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    # Missing DATABASE_URL, no ALLOW_SQLITE_DEV -> must refuse to start.
    p = _import_config({**base_env, "ADMIN_TOKEN": "t", "SIGNING_SECRET_ENCRYPTION_KEY": "11" * 32})
    check("missing DATABASE_URL fails fast",
          p.returncode != 0 and "DATABASE_URL" in p.stderr, p.stderr[-200:])
    # Missing encryption key -> must refuse to start.
    p = _import_config({**base_env, "ADMIN_TOKEN": "t", "DATABASE_URL": "sqlite:////tmp/x.db"})
    check("missing SIGNING_SECRET_ENCRYPTION_KEY fails fast",
          p.returncode != 0 and "SIGNING_SECRET_ENCRYPTION_KEY" in p.stderr, p.stderr[-200:])
    # Malformed encryption key -> must refuse to start.
    p = _import_config({**base_env, "ADMIN_TOKEN": "t", "DATABASE_URL": "sqlite:////tmp/x.db",
                        "SIGNING_SECRET_ENCRYPTION_KEY": "not-hex"})
    check("malformed SIGNING_SECRET_ENCRYPTION_KEY fails fast",
          p.returncode != 0 and "64 hex characters" in p.stderr, p.stderr[-200:])
    # ALLOW_SQLITE_DEV=1 opts into the local default explicitly.
    p = _import_config({**base_env, "ADMIN_TOKEN": "t", "SIGNING_SECRET_ENCRYPTION_KEY": "11" * 32,
                        "ALLOW_SQLITE_DEV": "1"})
    check("ALLOW_SQLITE_DEV=1 allows the SQLite default",
          p.returncode == 0 and p.stdout.strip() == "sqlite:///./heart.db", p.stderr[-200:])
    # Explicit DATABASE_URL is accepted as-is.
    p = _import_config({**base_env, "ADMIN_TOKEN": "t", "SIGNING_SECRET_ENCRYPTION_KEY": "11" * 32,
                        "DATABASE_URL": "sqlite:////tmp/x.db"})
    check("explicit DATABASE_URL is accepted",
          p.returncode == 0 and p.stdout.strip() == "sqlite:////tmp/x.db", p.stderr[-200:])

    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
