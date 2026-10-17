from __future__ import annotations

from datetime import UTC, datetime

from career_copilot.domain.models import (
    FilterOutcome,
    JobStatus,
    ScoreStage,
    StoredJob,
)
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoredJob, ScoreVerdict, rank
from tests.conftest import make_raw

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def make_scored(job_id: int, score: int, seen: datetime = NOW) -> ScoredJob:
    stored = StoredJob(
        id=job_id,
        posting=to_posting(make_raw(source_job_id=str(job_id))),
        filter_outcome=FilterOutcome.PASS,
        filter_reasons=(),
        filter_warnings=(),
        status=JobStatus.ACTIVE,
        first_seen_at=NOW,
        last_seen_at=seen,
    )
    return ScoredJob(
        job=stored,
        score=Score(
            job_id=job_id,
            stage=ScoreStage.MATCH,
            score=score,
            verdict=ScoreVerdict.POSSIBLE,
            rubric_version=RUBRIC_VERSION,
            model="fake",
            prompt_hash="p",
            content_hash="c",
        ),
    )


def test_rank_orders_by_score_descending() -> None:
    ranked = rank([make_scored(1, 40), make_scored(2, 90), make_scored(3, 65)])
    assert [s.score.score for s in ranked] == [90, 65, 40]


def test_rank_breaks_ties_by_last_seen_then_id() -> None:
    older = NOW.replace(day=10)
    ranked = rank([make_scored(1, 80, older), make_scored(2, 80, NOW), make_scored(3, 80, NOW)])
    assert [s.job.id for s in ranked] == [2, 3, 1]


def test_rank_is_stable_across_calls() -> None:
    jobs = [make_scored(1, 80), make_scored(2, 80), make_scored(3, 80)]
    assert [s.job.id for s in rank(jobs)] == [s.job.id for s in rank(list(reversed(jobs)))]


def test_verdict_from_score_uses_documented_bands() -> None:
    assert ScoreVerdict.from_score(85) is ScoreVerdict.STRONG
    assert ScoreVerdict.from_score(70) is ScoreVerdict.STRONG
    assert ScoreVerdict.from_score(69) is ScoreVerdict.POSSIBLE
    assert ScoreVerdict.from_score(45) is ScoreVerdict.POSSIBLE
    assert ScoreVerdict.from_score(44) is ScoreVerdict.WEAK
