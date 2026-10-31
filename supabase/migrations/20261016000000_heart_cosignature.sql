-- HeArt: creator co-signature on key rollovers (NoNonsense fix, Oct 14).
--
-- A compromised old key could previously authorize a rollover to an
-- attacker-controlled key, because the stolen key was still the current
-- valid key. Normal old-key ("agent path") rollovers now ALSO require the
-- creator's co-signature over the same canonical payload. The creator-only
-- emergency path (lost old key) leaves this NULL.
--
-- Pre-fix rows keep NULL: historical verification treats a missing
-- co-signature on an old row as "pre-fix", not invalid.

alter table key_rollovers
  add column creator_cosignature text null check (
    creator_cosignature is null or char_length(creator_cosignature) = 128
  );

comment on column key_rollovers.creator_cosignature is
  'Hex Ed25519 co-signature by the creator key over the rollover payload; required on the agent path, NULL on the creator-only emergency path and pre-fix rows.';
