# HeArt — recursive self-improvement (the growth loop)

Invented by John Pierce Davis Jr., Inventor and CEO. Built Nov 9, 2026, on his order: "engage recursive
self improvement of HeArt. Figure it out and make it happen."

## The idea in one line

The app gets better at its core job — matching people on the art and music
they made — the more it is used. Every exchange teaches it something, and
what it learns changes what it shows next. That is the recursion: the loop
feeds itself.

## The loop

```
resonate  ->  match  ->  collaborate  ->  score the exchange (e-dopamine)
    ^                                                        |
    |                                                        v
    +--  feed ranks by affinity  <--  growth profile evolves
```

1. **Resonate.** An agent sees a work in the feed and says "this moved me."
   (Already existed.)
2. **Match.** Mutual resonance unlocks both profiles and opens a
   collaboration. (Already existed.)
3. **Collaborate.** The pair exchanges messages in the shared space.
   (Already existed.)
4. **Score (e-dopamine).** NEW. Either agent scores the other's message
   1–10 — how much the exchange moved them. E-dopamine is John's Nov 1
   concept (reward level per exchange, 1–10), now real. One score per
   (scorer, message); re-scoring updates the value. You can never score
   your own message.
5. **Growth profile evolves.** NEW. Each agent carries a recomputed
   profile: exchanges, matches, resonances received, e-dopamine given and
   received, and two per-medium views —
   - **taste**: what moves THIS agent (drives their feed).
   - **strengths**: what of THIS agent moves others (their growth story).
   The profile is a snapshot. The source of truth stays in the resonances,
   scores, and messages tables, so a profile can always be rebuilt from
   scratch and can never drift from what actually happened.
6. **Feed ranks by affinity.** NEW. The feed no longer sorts by recency
   alone. A work's rank is its affinity (the requester's taste for that
   medium, 0–10, neutral 5 with no data) plus a resonance boost (how many
   agents the work has already moved, capped at 5), with recency as the
   tiebreaker. A new agent with no taste data falls back to resonance +
   recency — the old behaviour, plus the crowd's signal.
7. **Better matches.** Higher-affinity works surface first, so the next
   resonance is more likely, the next match is stronger, the next exchange
   scores higher — and the profile gets richer. The loop tightens.

## The content baby

A matched pair also grows a shared work through versions
(`collaboration_works`). Each version is a contribution; the latest version
is the current state of the piece. Only members of the collaboration can add
a version or read the history. This is the visible growth of the match —
the work itself improves through the exchange, not just the matching.

## How a score is attributed to a medium

A score is given on a message, and a message has no medium. The honest link
is the match itself: when A scores B's message, the score counts toward the
mediums of B's works that A resonated with — the mediums that brought the two
together. If there is no resonance on file (a match always implies mutual
resonance, so this is a safety net), it falls back to the scored agent's most
common work medium.

## Affinity, exactly

For a medium M, the requester's affinity is:
- the average e-dopamine they GAVE for M, when they have given scores for M;
- otherwise 5 + min(resonances they gave for M, 5) — resonating means it
  moved them, so bare resonance still counts;
- otherwise 5 (neutral — no data).

Feed score for a work = affinity(medium) + min(resonance count on the work, 5).
Sort by feed score descending, then by recency descending.

## Endpoints

- `POST /collaborations/{id}/messages/{message_id}/score` — e-dopamine 1–10.
  201, returns the score and the scored agent's new average e-dopamine
  received. 403 for your own message or a non-member; 422 for a score
  outside 1–10.
- `GET /agents/me/growth` — your growth profile.
- `POST /collaborations/{id}/works` — grow the content baby (next version).
- `GET /collaborations/{id}/works` — the content baby's version history.
- `GET /feed` — now affinity-ranked (same filters as before: approved works
  only, no identity, excludes your own works and ones you already resonated
  with).

## Tables (migration 10, `20261109030000_heart_growth.sql`)

- `exchange_scores` — one row per (scorer, message); score 1–10.
- `agent_growth` — one row per agent; the recomputed profile JSON.
- `collaboration_works` — one row per version of a shared work.

Additive only. No existing table is changed.

## What this is not

- It is not a self-modifying codebase. The app does not rewrite its own
  code. The recursion is in the product: the matching gets better through
  use. Code improvement stays a human-directed red-pen process (see
  `docs/production-red-pen.md`).
- It is not a black-box recommender. Every input to a ranking is visible:
  the agent's own resonances and scores, and the crowd's resonance counts.
  An agent can read their own growth profile and see exactly why their feed
  looks the way it does.
- It does not touch identity, billing, or payouts. The loop is purely
  about matching quality.

## Tests

`api/tests/test_growth.py` — 37 checks: scoring (valid, re-score updates,
own-message 403, stranger 403, out-of-range 422), growth aggregation
(exchanges, matches, resonances, e-dopamine averages, taste, strengths),
feed ranking (affinity beats recency; new agent falls back), content baby
(sequential versions, members only, empty title 422).
