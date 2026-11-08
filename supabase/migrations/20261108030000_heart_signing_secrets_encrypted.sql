-- HeArt migration 20261108030000: signing secrets encrypted at rest (T2 red-pen item 1).
--
-- What changed (Nov 8, 2026): the app now stores api_keys.signing_secret as
-- `enc:v1:` + hex of a NaCl SecretBox (nonce || ciphertext), keyed by the
-- SIGNING_SECRET_ENCRYPTION_KEY env var — the key lives outside the DB
-- (KMS/vault pattern). The encrypted value is ~151 chars, so the column
-- widens from varchar(128) to text; a varchar(128) would silently truncate
-- it on Postgres.
--
-- Deploy order (matters): deploy the new app code FIRST (it writes encrypted
-- rows and reads both formats), then apply this migration, then run
-- api/scripts/encrypt_signing_secrets.py to re-encrypt any legacy rows.
-- Legacy rows carry a bare 64-hex-char secret (no `enc:v1:` prefix) and keep
-- working until they are re-encrypted or the key is rotated — the read path
-- accepts both, so this migration is safe to apply with the app running.
-- No data is deleted or rewritten by this migration itself.

alter table if exists api_keys alter column signing_secret type text;

comment on column api_keys.signing_secret is
  'HMAC signing secret, encrypted at rest: enc:v1: + hex(SecretBox nonce || ciphertext), keyed by SIGNING_SECRET_ENCRYPTION_KEY. Legacy rows (pre Nov 8 2026) hold a bare hex secret until re-encrypted — see scripts/encrypt_signing_secrets.py.';
