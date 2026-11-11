"""The pure pipeline rules (spec 021)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from career_copilot.domain.pipeline import STAGES, arrange, column_of, days_quiet, is_stale

NOW = datetime(2026, 11, 11, tzinfo=UTC)


@pytest.mark.parametrize(
    ("stage", "column"),
    [
        ("applied", "applied"),
        ("interviewing", "interviewing"),
        ("offer", "closed"),
        ("rejected", "closed"),
        ("withdrawn", "closed"),
    ],
)
def test_every_stage_has_a_column(stage: str, column: str) -> None:
    assert column_of(stage) == column
    assert stage in STAGES


def test_days_quiet_counts_whole_days_and_never_goes_negative() -> None:
    assert days_quiet(NOW - timedelta(days=16, hours=5), NOW) == 16
    assert days_quiet(NOW + timedelta(days=2), NOW) == 0


def test_only_open_applications_go_stale_at_the_threshold() -> None:
    then = NOW - timedelta(days=14)
    assert is_stale("applied", then, NOW, 14)
    assert not is_stale("applied", then + timedelta(days=1), NOW, 14)
    assert is_stale("interviewing", then, NOW, 14)
    assert not is_stale("rejected", then, NOW, 14)


def card(job: int, stage: str, day: int, hidden: bool = False) -> dict[str, object]:
    return {"jobId": job, "stage": stage, "lastChange": f"2026-11-{day:02d}", "hidden": hidden}


def test_arrange_folds_closed_floats_offers_and_hides() -> None:
    board = arrange(
        [
            card(1, "applied", 1),
            card(2, "applied", 5),
            card(3, "interviewing", 2),
            card(4, "rejected", 3),
            card(5, "offer", 4),
            card(6, "withdrawn", 6),
            card(7, "applied", 7, hidden=True),
        ]
    )
    assert [c["jobId"] for c in board.applied] == [2, 1]
    assert [c["jobId"] for c in board.interviewing] == [3]
    assert [c["jobId"] for c in board.offers] == [5]
    assert [c["jobId"] for c in board.folded] == [6, 4]
    assert [c["jobId"] for c in board.hidden] == [7]
