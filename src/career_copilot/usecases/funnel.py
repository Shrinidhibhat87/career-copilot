"""The run funnel: what discovery and scoring did, in one JSON-ready dict (spec 013)."""

from __future__ import annotations

from typing import Any

from career_copilot.domain.models import DiscoverReport, ScoreReport


def _skipped(entry: str) -> dict[str, str]:
    """`source:id (reason)` as the discover run reports it, split into target and reason."""
    target, _, reason = entry.partition(" (")
    return {"target": target, "reason": reason.rstrip(")")}


def stopped_by(score: ScoreReport) -> str:
    """The one limit that ended the scoring stage."""
    if score.aborted:
        return "budget"
    if score.above_threshold >= score.target:
        return "target reached"
    pool = score.candidates - score.capped
    if score.matched + score.failed >= pool:
        return "out of candidates"
    return "match-call cap"


def build_funnel(discover: DiscoverReport, score: ScoreReport, min_score: int) -> dict[str, Any]:
    failed = [
        {"target": f"{run.source}:{run.identifier}", "error": run.error or ""}
        for run in discover.source_runs
        if not run.ok
    ]
    searched = len(discover.source_runs)
    return {
        "discover": {
            "targets": searched + len(discover.skipped_targets),
            "searched": searched,
            "fetched": discover.fetched,
            "inserted": discover.inserted,
            "rejected": discover.rejected,
            "review": discover.manual_review,
            "rejectReasons": [
                {"reason": reason, "count": count} for reason, count in discover.reject_reasons[:8]
            ],
            "qualified": discover.qualified,
            "boards": [
                {"target": f"{run.source}:{run.identifier}", "fetched": run.fetched}
                for run in discover.source_runs
                if run.ok
            ],
            "skipped": [_skipped(entry) for entry in discover.skipped_targets],
            "failed": failed,
        },
        "score": {
            "candidates": score.candidates,
            "capped": score.capped,
            "matched": score.matched,
            "above": score.above_threshold,
            "target": score.target,
            "minScore": min_score,
            "failed": score.failed,
            "stoppedBy": stopped_by(score),
        },
    }
