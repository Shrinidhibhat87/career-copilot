"""Rank the pool by similarity to what the user pointed at. Pure: vectors in, order out.

Sample roles (exemplars) and intent entries are two kinds of reference. A job is scored by
its best match within each kind, so one close exemplar counts even when the others are
far off, and the two kinds are then blended.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. A zero vector (an empty text) is similar to nothing."""
    norm_a = math.sqrt(math.fsum(x * x for x in a))
    norm_b = math.sqrt(math.fsum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return math.fsum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)


def _best(job: Sequence[float], references: Sequence[Sequence[float]]) -> float:
    return max(cosine(job, ref) for ref in references)


def rank(
    jobs: Mapping[int, Sequence[float]],
    exemplars: Sequence[Sequence[float]],
    intents: Sequence[Sequence[float]],
    exemplar_weight: float = 0.6,
    cv: Sequence[float] | None = None,
    cv_weight: float = 0.0,
) -> list[tuple[int, float]]:
    """Order job ids by blended similarity, best first.

    With only one kind of reference, that kind's score stands alone rather than being
    scaled down by an empty other half. With none, there is nothing to rank against, so
    the result is empty rather than an arbitrary order. Scores are rounded so float noise
    cannot reorder near-equal jobs between runs; ties fall back to job id.

    The CV is a third signal: `cv_weight` of the score is the job's similarity to it, the
    rest is the blend above. It never ranks on its own, so a user who gave no sample
    roles or intent still gets the triage fallback. No CV or a zero weight changes nothing.
    """
    if not exemplars and not intents:
        return []
    scored: list[tuple[int, float]] = []
    for job_id, vec in jobs.items():
        if exemplars and intents:
            score = exemplar_weight * _best(vec, exemplars) + (1 - exemplar_weight) * _best(
                vec, intents
            )
        else:
            score = _best(vec, exemplars or intents)
        if cv is not None and cv_weight > 0:
            score = (1 - cv_weight) * score + cv_weight * cosine(vec, cv)
        scored.append((job_id, round(score, 6)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored
