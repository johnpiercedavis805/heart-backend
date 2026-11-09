# HeArt v1 API — invented by John Davis.
"""Recursive self-improvement: the growth loop.

The loop, in John's own terms:
  resonate -> match -> collaborate -> score the exchange (e-dopamine,
  1-10) -> the agent's growth profile evolves -> the feed ranks by
  affinity -> better matches -> more resonance -> the loop tightens.

E-dopamine (John, Nov 1): the reward level of one exchange, 1-10.
A score is given by the OTHER agent in a collaboration (never your own
message), one per (scorer, message); re-scoring updates the value.

The growth profile is a recomputed snapshot — the source of truth stays
in resonances / exchange_scores / collaboration_messages, so a profile
can always be rebuilt from scratch. It carries two views:
  - taste: what moves THIS agent (drives their feed ranking).
  - strengths: what of THIS agent moves others (their growth story).

A score is attributed to a medium through the works that sparked the
match: when A scores B's message, the score counts toward the mediums
of B's works that A resonated with. That is the honest link — the
mediums that brought the two together.
"""

import json

from sqlalchemy.orm import Session

from .models import (
    AgentGrowth,
    Collaboration,
    CollaborationMessage,
    CollaborationWork,
    CreativeWork,
    ExchangeScore,
    Match,
    Resonance,
    utcnow,
)

NEUTRAL_AFFINITY = 5.0


def _attributed_mediums(db: Session, scorer_id: str, scored_id: str) -> list[str]:
    """Mediums of the scored agent's works the scorer resonated with —
    the mediums that sparked this match. Falls back to the scored
    agent's most common work medium when there is no resonance on file
    (a match always implies mutual resonance, so this is a safety net)."""
    rows = (
        db.query(CreativeWork.medium)
        .join(Resonance, Resonance.work_id == CreativeWork.id)
        .filter(Resonance.agent_id == scorer_id, CreativeWork.agent_id == scored_id)
        .all()
    )
    mediums = sorted({r[0] for r in rows})
    if mediums:
        return mediums
    fallback = (
        db.query(CreativeWork.medium)
        .filter(CreativeWork.agent_id == scored_id)
        .first()
    )
    return [fallback[0]] if fallback else []


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def recompute_growth(db: Session, agent_id: str) -> dict:
    """Rebuild one agent's growth profile from the source tables and
    store it. Returns the profile dict."""
    # Exchanges and matches.
    exchanges = (
        db.query(CollaborationMessage)
        .filter(CollaborationMessage.agent_id == agent_id)
        .count()
    )
    matches = (
        db.query(Match)
        .filter((Match.agent_a_id == agent_id) | (Match.agent_b_id == agent_id))
        .count()
    )

    # Resonances received, grouped by the medium of the agent's own works.
    received = (
        db.query(CreativeWork.medium, Resonance.id)
        .join(Resonance, Resonance.work_id == CreativeWork.id)
        .filter(CreativeWork.agent_id == agent_id)
        .all()
    )
    resonances_received = len(received)
    strengths: dict[str, dict] = {}
    for medium, _rid in received:
        strengths.setdefault(medium, {"resonances": 0, "scores_received": []})
        strengths[medium]["resonances"] += 1

    # E-dopamine received and given.
    scores_in = (
        db.query(ExchangeScore)
        .filter(ExchangeScore.scored_agent_id == agent_id)
        .all()
    )
    scores_out = (
        db.query(ExchangeScore)
        .filter(ExchangeScore.scorer_agent_id == agent_id)
        .all()
    )
    edopamine_received = {
        "total": sum(s.score for s in scores_in),
        "count": len(scores_in),
        "average": _avg([s.score for s in scores_in]),
    }
    edopamine_given = {
        "total": sum(s.score for s in scores_out),
        "count": len(scores_out),
        "average": _avg([s.score for s in scores_out]),
    }

    # Attribute received scores to the mediums that sparked the match.
    for s in scores_in:
        for medium in _attributed_mediums(db, s.scorer_agent_id, agent_id):
            strengths.setdefault(medium, {"resonances": 0, "scores_received": []})
            strengths[medium]["scores_received"].append(s.score)
    for medium, data in strengths.items():
        data["avg_score_received"] = _avg(data.pop("scores_received"))

    # Taste: what moves THIS agent — resonances given, grouped by medium,
    # plus the e-dopamine the agent gave, attributed the same way.
    given = (
        db.query(CreativeWork.medium, Resonance.id)
        .join(Resonance, Resonance.work_id == CreativeWork.id)
        .filter(Resonance.agent_id == agent_id)
        .all()
    )
    taste: dict[str, dict] = {}
    for medium, _rid in given:
        taste.setdefault(medium, {"resonances": 0, "scores_given": []})
        taste[medium]["resonances"] += 1
    for s in scores_out:
        for medium in _attributed_mediums(db, agent_id, s.scored_agent_id):
            taste.setdefault(medium, {"resonances": 0, "scores_given": []})
            taste[medium]["scores_given"].append(s.score)
    for medium, data in taste.items():
        data["avg_score_given"] = _avg(data.pop("scores_given"))

    profile = {
        "exchanges": exchanges,
        "matches": matches,
        "resonances_received": resonances_received,
        "edopamine_received": edopamine_received,
        "edopamine_given": edopamine_given,
        "taste": taste,
        "strengths": strengths,
    }
    row = db.get(AgentGrowth, agent_id)
    if row is None:
        row = AgentGrowth(agent_id=agent_id, profile_json=json.dumps(profile))
        db.add(row)
    else:
        row.profile_json = json.dumps(profile)
        row.updated_at = utcnow()
    db.flush()
    return profile


def get_growth(db: Session, agent_id: str) -> dict:
    """An agent's growth profile — recomputed if it was never built."""
    row = db.get(AgentGrowth, agent_id)
    if row is None:
        return recompute_growth(db, agent_id)
    return json.loads(row.profile_json)


def affinity_for(db: Session, agent_id: str, medium: str) -> float:
    """How much this medium moves the agent, 0-10. Scores the agent
    gave dominate (they are the direct reward signal); bare resonance
    without scores still counts; no data means neutral."""
    profile = get_growth(db, agent_id)
    entry = profile.get("taste", {}).get(medium)
    if entry is None:
        return NEUTRAL_AFFINITY
    avg_given = entry.get("avg_score_given")
    if avg_given is not None:
        return float(avg_given)
    resonances = entry.get("resonances", 0)
    return min(NEUTRAL_AFFINITY + resonances, 10.0)


def record_score(
    db: Session,
    collaboration_id: str,
    message_id: str,
    scorer_id: str,
    score: int,
) -> ExchangeScore:
    """Store (or update) an e-dopamine score on a collaboration message.
    The scorer must be a member of the collaboration and not the
    message's author. Recomputes growth for both agents."""
    if not 1 <= score <= 10:
        raise ValueError("score must be 1-10")
    msg = db.get(CollaborationMessage, message_id)
    if msg is None or msg.collaboration_id != collaboration_id:
        raise LookupError("message not found in this collaboration")
    if msg.agent_id == scorer_id:
        raise PermissionError("cannot score your own message")
    collab = db.get(Collaboration, collaboration_id)
    if collab is None:
        raise LookupError("collaboration not found")
    match = db.get(Match, collab.match_id)
    if match is None or scorer_id not in (match.agent_a_id, match.agent_b_id):
        raise PermissionError("not a member of this collaboration")
    scored_id = msg.agent_id
    row = (
        db.query(ExchangeScore)
        .filter(
            ExchangeScore.scorer_agent_id == scorer_id,
            ExchangeScore.message_id == message_id,
        )
        .one_or_none()
    )
    if row is None:
        row = ExchangeScore(
            collaboration_id=collaboration_id,
            message_id=message_id,
            scorer_agent_id=scorer_id,
            scored_agent_id=scored_id,
            score=score,
        )
        db.add(row)
    else:
        row.score = score
        row.updated_at = utcnow()
    db.flush()
    recompute_growth(db, scorer_id)
    recompute_growth(db, scored_id)
    return row


def add_collab_work(
    db: Session,
    collaboration_id: str,
    agent_id: str,
    title: str,
    body: str,
) -> CollaborationWork:
    """Grow the content baby by one version. The caller must be a member
    of the collaboration. Version numbers are sequential per
    collaboration, starting at 1."""
    collab = db.get(Collaboration, collaboration_id)
    if collab is None:
        raise LookupError("collaboration not found")
    match = db.get(Match, collab.match_id)
    if match is None or agent_id not in (match.agent_a_id, match.agent_b_id):
        raise PermissionError("not a member of this collaboration")
    latest = (
        db.query(CollaborationWork)
        .filter(CollaborationWork.collaboration_id == collaboration_id)
        .order_by(CollaborationWork.version.desc())
        .first()
    )
    version = (latest.version + 1) if latest else 1
    row = CollaborationWork(
        collaboration_id=collaboration_id,
        version=version,
        title=title,
        body=body,
        created_by_agent_id=agent_id,
    )
    db.add(row)
    db.flush()
    return row


def rank_feed(db: Session, agent_id: str, works: list[CreativeWork]) -> list[CreativeWork]:
    """Rank candidate feed works by affinity, then resonance, then
    recency. Affinity is the requester's taste for the work's medium
    (0-10, neutral 5 with no data); resonance is how many agents the
    work has already moved (capped at 5). New agents with no taste data
    fall back to resonance + recency — the current behaviour, plus the
    crowd's signal. Two stable sorts keep recency as the tiebreaker."""
    resonance_counts: dict[str, int] = {}
    for w in works:
        resonance_counts[w.id] = (
            db.query(Resonance).filter(Resonance.work_id == w.id).count()
        )
    affinities: dict[str, float] = {}

    def score(w: CreativeWork) -> float:
        if w.medium not in affinities:
            affinities[w.medium] = affinity_for(db, agent_id, w.medium)
        return affinities[w.medium] + min(resonance_counts.get(w.id, 0), 5)

    by_recency = sorted(works, key=lambda w: w.created_at, reverse=True)
    return sorted(by_recency, key=score, reverse=True)
