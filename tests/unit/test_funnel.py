from __future__ import annotations

import pytest

from career_copilot.domain.models import DiscoverReport, ScoreReport, SourceRunReport
from career_copilot.usecases.funnel import build_funnel, stopped_by


def score(**fields: object) -> ScoreReport:
    base: dict[str, object] = {
        "run_id": 1,
        "candidates": 39,
        "capped": 19,
        "matched": 19,
        "above_threshold": 18,
        "target": 50,
    }
    return ScoreReport(**{**base, **fields})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"aborted": True}, "budget"),
        ({"above_threshold": 50}, "target reached"),
        ({"matched": 19, "failed": 1}, "out of candidates"),
        ({"matched": 12, "failed": 0}, "match-call cap"),
    ],
)
def test_stopped_by_names_the_limit_that_ended_scoring(
    fields: dict[str, object], expected: str
) -> None:
    assert stopped_by(score(**fields)) == expected


def test_the_funnel_counts_searched_skipped_and_failed_targets() -> None:
    report = DiscoverReport(
        run_id=1,
        fetched=345,
        inserted=40,
        qualified=28,
        rejected=290,
        manual_review=15,
        reject_reasons=(("location outside Germany", 3), ("no profile-stack signal", 1)),
        skipped_targets=("greenhouse:n26 (cooldown)", "greenhouse:celonis (target met)"),
        source_runs=(
            SourceRunReport("greenhouse", "helsing", ok=True, fetched=100),
            SourceRunReport("greenhouse", "raisin", ok=False, error="timeout"),
        ),
    )
    funnel = build_funnel(report, score(failed=1), min_score=30)
    assert funnel["discover"]["targets"] == 4
    assert funnel["discover"]["searched"] == 2
    assert funnel["discover"]["skipped"] == [
        {"target": "greenhouse:n26", "reason": "cooldown"},
        {"target": "greenhouse:celonis", "reason": "target met"},
    ]
    assert funnel["discover"]["failed"] == [{"target": "greenhouse:raisin", "error": "timeout"}]
    assert funnel["discover"]["boards"] == [{"target": "greenhouse:helsing", "fetched": 100}]
    assert funnel["discover"]["rejectReasons"] == [
        {"reason": "location outside Germany", "count": 3},
        {"reason": "no profile-stack signal", "count": 1},
    ]
    assert funnel["score"]["minScore"] == 30
    assert funnel["score"]["above"] == 18
    assert funnel["score"]["stoppedBy"] == "out of candidates"


def test_main_reason_groups_a_rejection_by_its_first_reason_without_detail() -> None:
    from career_copilot.usecases.discover import main_reason

    assert (
        main_reason(["location outside Germany: Geneva, Switzerland", "no profile-stack signal"])
        == "location outside Germany"
    )
    assert main_reason(["requires German at C1 (profile is B1)"]) == "requires German at C1"
    assert main_reason([]) == "no reason recorded"
