"""Scoring domain: value types and deterministic ranking. Stdlib only, zero I/O."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from career_copilot.domain.models import ScoreStage, StoredJob

# Bump when the rubric wording or the output contract changes. A bump invalidates
# every existing score, because the unique index in `scores` includes it.
RUBRIC_VERSION = 3

# The four named parts of a match score and the most each may award. They sum to 100, so
# the total is the sum of the parts and never a separate number from the model.
PART_MAX: dict[str, int] = {"skills": 40, "seniority": 25, "domain": 20, "preferences": 15}

# Seniority only earns its full points when skills coverage is at least this share of the
# skills maximum; below it, seniority is capped at SENIORITY_GATED_SHARE of its maximum.
SENIORITY_GATE_SKILLS_MIN = 0.5
SENIORITY_GATED_SHARE = 0.4


class ScoreVerdict(StrEnum):
    """Coarse band over the numeric score, so the digest can group without a threshold."""

    STRONG = "strong"
    POSSIBLE = "possible"
    WEAK = "weak"

    @classmethod
    def from_score(cls, score: int) -> ScoreVerdict:
        if score >= 70:
            return cls.STRONG
        if score >= 45:
            return cls.POSSIBLE
        return cls.WEAK


@dataclass(frozen=True, slots=True)
class Score:
    """One scoring result with the stamp that decides whether it is still valid."""

    job_id: int
    stage: ScoreStage
    score: int
    verdict: ScoreVerdict
    rubric_version: int
    model: str
    prompt_hash: str
    content_hash: str
    reasons: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    created_at: str | None = None
    # Which preferences produced this score. The version answers "what did I hold then";
    # the hash answers "would this come out the same today", and it is the hash that joins
    # the identity index.
    preferences_version: int = 0
    preferences_hash: str = ""
    # Which CV produced this score. Provenance only: it is not part of the identity index,
    # so uploading a new CV never makes an existing score stale. None = scored before CVs
    # were versioned.
    cv_sha256: str | None = None
    # {"skills": {"points": 30, "reason": "..."}, ...}. None for a version 1 score and for
    # triage, which keep one total and no breakdown.
    parts: dict[str, dict[str, object]] | None = None


@dataclass(frozen=True, slots=True)
class ScoredJob:
    """A stored job with its match score, as the digest consumes it."""

    job: StoredJob
    score: Score
    triage_score: int | None = None
    triage_reason: str = ""


def rank(jobs: Iterable[ScoredJob]) -> tuple[ScoredJob, ...]:
    """Highest score first. Ties break by recency, then id, so runs are reproducible."""
    return tuple(
        sorted(jobs, key=lambda s: (-s.score.score, -s.job.last_seen_at.timestamp(), s.job.id))
    )
