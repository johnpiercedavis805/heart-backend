# HeArt v1 API — invented by John Davis.
"""Key generation, hashing, and provenance signing.

Design (§2 of the spec): the API key authenticates; a separate per-key
*signing secret* (generated at issuance, stored server-side) HMAC-signs file
hashes. Only the SHA-256 of an API key or invite code is ever stored — the
plaintext is shown exactly once and never again.

Signing secrets at rest (T2 red-pen item 1, fixed Nov 8 2026): the secret is
NEVER stored in plaintext. `encrypt_signing_secret` wraps it in a NaCl
SecretBox (XSalsa20-Poly1305, authenticated encryption) keyed by the
32-byte `SIGNING_SECRET_ENCRYPTION_KEY` env var — a key that lives outside
the database (KMS/vault pattern: the DB holds ciphertext, the key lives in
the deploy environment). Stored form: `enc:v1:` + hex(nonce || ciphertext).
`decrypt_signing_secret` reverses it; a wrong key or tampered value raises
instead of returning a secret. Rows written before this fix carry a bare
hex secret (no prefix) — the read path still accepts those so a migration
doesn't break existing keys; run `scripts/encrypt_signing_secrets.py` to
re-encrypt them, or rotate the key. Never expose a secret via any endpoint.
"""

import hashlib
import hmac
import secrets

from nacl.secret import SecretBox

from .config import settings

KEY_PREFIX = "hk_"
INVITE_PREFIX = "hi_"

# Versioned prefix marking an encrypted signing secret in the DB.
_ENC_PREFIX = "enc:v1:"


def _encryption_key() -> bytes:
    # config.py validates this at import (present, 64 hex chars, 32 bytes).
    return bytes.fromhex(settings.SIGNING_SECRET_ENCRYPTION_KEY)


def encrypt_signing_secret(plaintext_hex: str) -> str:
    """Encrypt a 256-bit signing secret for storage. Returns
    `enc:v1:` + hex(nonce || SecretBox ciphertext). A fresh random nonce
    per call, so the same secret encrypts differently every time."""
    box = SecretBox(_encryption_key())
    encrypted = box.encrypt(plaintext_hex.encode("utf-8"))
    return _ENC_PREFIX + encrypted.hex()


def decrypt_signing_secret(stored: str) -> str:
    """Recover a signing secret from its stored form. Prefixed values are
    SecretBox-decrypted (wrong key or tampered bytes raise). A bare hex
    value is a legacy row written before encryption-at-rest (Nov 8 2026)
    and is returned as-is so existing keys keep working until they are
    re-encrypted or rotated — see scripts/encrypt_signing_secrets.py."""
    if not stored.startswith(_ENC_PREFIX):
        return stored  # legacy plaintext row (pre-encryption); backfill it
    box = SecretBox(_encryption_key())
    return box.decrypt(bytes.fromhex(stored[len(_ENC_PREFIX):])).decode("utf-8")


def is_signing_secret_encrypted(stored: str) -> bool:
    """True when a stored value is an encrypted secret (not legacy plaintext)."""
    return stored.startswith(_ENC_PREFIX)


def new_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def new_invite_code() -> str:
    return INVITE_PREFIX + secrets.token_urlsafe(24)


def new_signing_secret() -> str:
    return secrets.token_hex(32)  # 256-bit, stored as hex


def hash_secret(value: str) -> str:
    """One-way hash for API keys and invite codes. Never store the plaintext."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sign_hash(sha_hex: str, signing_secret_hex: str) -> str:
    """HMAC-SHA256 over the file's SHA-256 hex digest. Tamper-evident
    'key X made this' — attribution, NOT originality proof (see spec §2)."""
    return hmac.new(
        bytes.fromhex(signing_secret_hex),
        sha_hex.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
