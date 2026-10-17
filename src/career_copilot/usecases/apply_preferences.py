"""Re-filter stored jobs after a preferences change.

Filter outcomes are computed at discover time and stored on the job row, so without this
a changed city list or seniority band would leave every stored job exactly as it was and
the screen would appear inert. Free: no model call, no network, no score row written.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from career_copilot.domain.filters import apply_hard_filters, matching_terms
from career_copilot.domain.models import FilterOutcome, HardFilterResult, StoredJob
from career_copilot.domain.preferences import Preferences
from career_copilot.ports import StateStore


@dataclass(frozen=True, slots=True)
class RefilterReport:
    """What the re-filter moved, and what the next scoring run would now re-open."""

    examined: int = 0
    changed: int = 0
    # Keyed "old→new", e.g. "reject→pass". Only transitions appear.
    transitions: dict[str, int] = field(default_factory=dict)
    reopened_for_scoring: int = 0

    @property
    def now_passing(self) -> int:
        return sum(
            count
            for key, count in self.transitions.items()
            if key.endswith(f"→{FilterOutcome.PASS}")
        )


@dataclass(frozen=True, slots=True)
class TermImpact:
    """What one excluded role term does to the jobs already stored."""

    matches: int  # stored jobs whose title contains the term
    newly_hidden: int  # of those, the ones not already rejected


@dataclass(frozen=True, slots=True)
class RefilterPlan:
    """The re-filter worked out but not written: what a save would do, and what a preview shows."""

    examined: int
    changes: tuple[tuple[StoredJob, HardFilterResult], ...]
    transitions: dict[str, int]
    excluded_role_terms: dict[str, TermImpact]

    @property
    def now_passing(self) -> int:
        return self._moved_to(FilterOutcome.PASS)

    @property
    def hidden(self) -> int:
        return self._moved_to(FilterOutcome.REJECT)

    def _moved_to(self, outcome: FilterOutcome) -> int:
        return sum(count for key, count in self.transitions.items() if key.endswith(f"→{outcome}"))


def plan_refilter(jobs: Sequence[StoredJob], prefs: Preferences) -> RefilterPlan:
    """Re-run the deterministic filters over `jobs` without touching the store.

    Shared by the save and the preview, so what the screen promises is what a save does.
    """
    config = prefs.to_filter_config()
    transitions: dict[str, int] = {}
    changes: list[tuple[StoredJob, HardFilterResult]] = []
    for job in jobs:
        verdict = apply_hard_filters(job.posting, config)
        if (
            verdict.outcome is job.filter_outcome
            and verdict.reasons == job.filter_reasons
            and verdict.warnings == job.filter_warnings
        ):
            continue
        if verdict.outcome is not job.filter_outcome:
            key = f"{job.filter_outcome}→{verdict.outcome}"
            transitions[key] = transitions.get(key, 0) + 1
        changes.append((job, verdict))
    return RefilterPlan(
        examined=len(jobs),
        changes=tuple(changes),
        transitions=transitions,
        excluded_role_terms=_term_impacts(jobs, prefs.excluded_role_terms),
    )


def _term_impacts(jobs: Sequence[StoredJob], terms: Sequence[str]) -> dict[str, TermImpact]:
    """Per term, matched against the title exactly as the filter does (whole words)."""
    impacts: dict[str, TermImpact] = {}
    for term in terms:
        matched = [job for job in jobs if matching_terms(job.posting.title, (term,))]
        impacts[term] = TermImpact(
            matches=len(matched),
            newly_hidden=sum(
                1 for job in matched if job.filter_outcome is not FilterOutcome.REJECT
            ),
        )
    return impacts


def apply_preferences(
    store: StateStore,
    prefs: Preferences,
    model: str,
    rubric_version: int,
) -> RefilterReport:
    """Re-run the deterministic filters over every active stored job."""
    plan = plan_refilter(store.all_active_jobs(), prefs)
    for job, verdict in plan.changes:
        store.update_filter_result(job.id, verdict)
    reopened = len(
        store.jobs_needing_score(model, rubric_version, preferences_hash=prefs.prompt_hash())
    )
    return RefilterReport(
        examined=plan.examined,
        changed=len(plan.changes),
        transitions=plan.transitions,
        reopened_for_scoring=reopened,
    )
