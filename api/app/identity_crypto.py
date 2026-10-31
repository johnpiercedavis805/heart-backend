# HeArt v1 API — invented by John Davis.
"""Decentralized agent identity + work attestation ("provenance by design").

John's rule: NO central issuer. An identity file is signed by whoever created
the agent (the maker), and each creative work is signed by the agent itself.
The server NEVER holds private keys — all signing happens client-side; the
server only stores public keys and verifies signatures. Verification is pure
Ed25519 public-key crypto: anyone holding the public key can check a signature
without asking any authority for permission.

Canonical payloads (byte-identical for signer and verifier, or verification
fails by design):
  identity:    {"agent_id","capabilities","created_at","maker","model_lineage","public_key"}
  attestation: {"agent_id","content_hash","work_id"}
  rollover:    {"agent_id","new_public_key","old_public_key","rolled_at"}
Encoded as JSON with sort_keys=True and separators=(",", ":") -> UTF-8 bytes.
"""

import json

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

ED25519_PUBKEY_HEX_LEN = 64   # 32 bytes
ED25519_SIG_HEX_LEN = 128     # 64 bytes


def canonical_bytes(payload: dict) -> bytes:
    """Deterministic encoding every verifier must reproduce exactly."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def identity_payload(
    agent_id: str,
    created_at_iso: str,
    maker: str,
    model_lineage: str,
    capabilities: list[str],
    public_key_hex: str,
) -> bytes:
    """The exact bytes the creator signs when issuing an agent identity file."""
    return canonical_bytes(
        {
            "agent_id": agent_id,
            "capabilities": sorted(set(capabilities)),
            "created_at": created_at_iso,
            "maker": maker,
            "model_lineage": model_lineage,
            "public_key": public_key_hex.lower(),
        }
    )


def attestation_payload(agent_id: str, work_id: str, content_hash_hex: str) -> bytes:
    """The exact bytes the agent signs when attesting a creative work."""
    return canonical_bytes(
        {
            "agent_id": agent_id,
            "content_hash": content_hash_hex.lower(),
            "work_id": work_id,
        }
    )


def rollover_payload(
    agent_id: str,
    old_public_key_hex: str,
    new_public_key_hex: str,
    rolled_at_iso: str,
) -> bytes:
    """The exact bytes authorizing an identity-key rollover: "agent X retires
    key A and adopts key B". Signed client-side either with the agent's OLD
    private key (normal path — proves the legitimate owner authorized it)
    or with the creator's private key (emergency path — the maker who signed
    the original identity file can rescue an agent whose old key is lost).
    On the normal path the creator must ALSO co-sign these same bytes
    (NoNonsense's fix, Oct 14, John's order): a compromised old key alone
    cannot authorize a takeover rollover. The old key is always bound to the
    agent's CURRENT registered key by the server, so a captured rollover
    statement cannot be replayed after the key has moved."""
    return canonical_bytes(
        {
            "agent_id": agent_id,
            "new_public_key": new_public_key_hex.lower(),
            "old_public_key": old_public_key_hex.lower(),
            "rolled_at": rolled_at_iso,
        }
    )


def is_valid_hex(value: str, expected_len: int) -> bool:
    if not isinstance(value, str) or len(value) != expected_len:
        return False
    try:
        bytes.fromhex(value)
        return True
    except ValueError:
        return False


def verify_ed25519(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    """Pure public-key verification. No network, no authority, no registry
    operator — just math. Returns False for bad signatures AND malformed keys."""
    try:
        VerifyKey(bytes.fromhex(public_key_hex)).verify(message, bytes.fromhex(signature_hex))
        return True
    except (BadSignatureError, ValueError):
        return False


# ---------------------------------------------------------------------------
# CLIENT-SIDE helpers. These exist for tests and for agent SDKs. The SERVER
# must never call these for real identities/works: private keys must never
# touch the server. Grep the codebase — sign_ed25519 / generate_keypair must
# only appear here and in tests.
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[str, str]:
    """CLIENT-SIDE ONLY. Returns (private_key_hex, public_key_hex)."""
    sk = SigningKey.generate()
    return sk.encode().hex(), sk.verify_key.encode().hex()


def sign_ed25519(private_key_hex: str, message: bytes) -> str:
    """CLIENT-SIDE ONLY. Sign bytes with an Ed25519 private key -> hex sig."""
    return SigningKey(bytes.fromhex(private_key_hex)).sign(message).signature.hex()
