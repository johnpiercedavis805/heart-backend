# HeArt v1 API — invented by John Davis.
"""Key generation, hashing, and provenance signing.

Design (§2 of the spec): the API key authenticates; a separate per-key
*signing secret* (generated at issuance, stored server-side) HMAC-signs file
hashes. Only the SHA-256 of an API key or invite code is ever stored — the
plaintext is shown exactly once and never again. PRODUCTION NOTE: move
signing secrets to a KMS/vault; never expose them via any endpoint.
"""

import hashlib
import hmac
import secrets

KEY_PREFIX = "hk_"
INVITE_PREFIX = "hi_"


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
