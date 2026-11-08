# HeArt v1 API — invented by John Davis.
"""One-time backfill: re-encrypt legacy plaintext signing secrets (T2 red-pen item 1).

Rows written before Nov 8, 2026 carry a bare 64-hex-char secret in
api_keys.signing_secret. This script encrypts each of them in place with
the SIGNING_SECRET_ENCRYPTION_KEY env var (NaCl SecretBox — see app/crypto.py)
so no plaintext secret remains in the database.

Usage:
    cd api && DATABASE_URL=... ADMIN_TOKEN=... SIGNING_SECRET_ENCRYPTION_KEY=... \
        ../.venv/bin/python scripts/encrypt_signing_secrets.py

The app reads both formats, so it is safe to run with the app stopped or
running — but run it BEFORE production, and rotate any key you cannot
re-encrypt. Idempotent: already-encrypted rows are skipped. Prints counts;
exits non-zero if a row cannot be decrypted after writing (verification).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crypto  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ApiKey  # noqa: E402


def backfill(db) -> tuple[int, int]:
    """Encrypt every legacy (unprefixed) signing secret in place.
    Returns (encrypted_count, already_encrypted_count)."""
    rows = db.query(ApiKey).all()
    encrypted = 0
    skipped = 0
    for row in rows:
        if crypto.is_signing_secret_encrypted(row.signing_secret):
            skipped += 1
            continue
        plaintext = row.signing_secret
        row.signing_secret = crypto.encrypt_signing_secret(plaintext)
        # Verify the roundtrip before committing anything.
        assert crypto.decrypt_signing_secret(row.signing_secret) == plaintext
        encrypted += 1
    db.commit()
    return encrypted, skipped


def main() -> int:
    db = SessionLocal()
    try:
        encrypted, skipped = backfill(db)
    finally:
        db.close()
    print(f"signing secrets re-encrypted: {encrypted}; already encrypted: {skipped}")
    if encrypted:
        print("No plaintext signing secrets remain in api_keys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
