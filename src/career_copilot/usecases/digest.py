"""Digest: query plus template. Fully deterministic, no model call."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from career_copilot.domain.diversity import cap_per_company
from career_copilot.domain.models import DigestStats, FilterOutcome, StoredJob
from career_copilot.domain.runs import Progress
from career_copilot.domain.scoring import ScoredJob, rank
from career_copilot.ports import Notifier, StateStore

TITLE = "career-copilot digest"


def _bullets(label: str, values: Sequence[str]) -> list[str]:
    if not values:
        return []
    return [f"- **{label}:** " + "; ".join(values)]


def render_digest(
    scored: Sequence[ScoredJob],
    review: Sequence[StoredJob],
    generated_at: datetime,
    stats: DigestStats | None = None,
    max_per_company: int = 2,
    limit: int | None = None,
) -> str:
    """Deterministic Markdown. Same inputs, same bytes."""
    lines = [f"# {TITLE}", "", f"Generated {generated_at.isoformat()}", ""]

    if not scored:
        lines += ["No scored jobs yet. Run `career-copilot score`.", ""]
    else:
        lines += ["## Ranked matches", ""]
        ranked = rank(scored)
        displayed = cap_per_company(
            ranked,
            limit if limit is not None else len(ranked),
            max_per_company,
            company=lambda entry: entry.job.posting.company_norm,
        )
        for entry in displayed:
            posting = entry.job.posting
            location = posting.location or "location not stated"
            lines += [
                f"### {entry.score.score} — {posting.title} · {posting.company}",
                "",
                f"{location} · [{posting.canonical_url}]({posting.canonical_url})",
                "",
            ]
            lines += _bullets("Why", entry.score.reasons)
            lines += _bullets("Gaps", entry.score.gaps)
            lines += _bullets("Warnings", entry.score.warnings)
            lines += _bullets("Evidence", entry.score.evidence_refs)
            lines += [""]

    if review:
        lines += ["## Manual review", "", "Eligibility or language wording was ambiguous.", ""]
        for job in review:
            lines += [
                f"- {job.posting.title} · {job.posting.company} — "
                f"{'; '.join(job.filter_warnings) or 'see posting'}",
                f"  {job.posting.canonical_url}",
            ]
        lines += [""]

    if stats is not None:
        lines += [
            "## Statistics",
            "",
            f"- Active jobs: {stats.active_jobs}",
            f"- Currently scored: {stats.scored_jobs}",
            f"- Awaiting manual review: {stats.manual_review}",
            "",
        ]
        if stats.failed_sources:
            lines += ["## Source failures", ""]
            lines += [f"- {failure}" for failure in stats.failed_sources]
            lines += [""]

        if stats.watchlist_unresolved or stats.watchlist_blocked:
            lines += ["## Watchlist coverage", ""]
            lines += [f"- {name}: blocked — {reason}" for name, reason in stats.watchlist_blocked]
            lines += [f"- {name}: no public board found" for name in stats.watchlist_unresolved]
            lines += [""]

    return "\n".join(lines)


class DigestRun:
    """Renders the digest and hands it to a notifier."""

    def __init__(
        self,
        store: StateStore,
        notifier: Notifier,
        clock: Callable[[], datetime],
        limit: int = 20,
        model: str | None = None,
        rubric_version: int | None = None,
        max_per_company: int = 2,
        preferences_hash: str | None = None,
        run_id: int | None = None,
        progress: Callable[[Progress], None] | None = None,
        cost_eur: float = 0,
        budget_eur: float = 0,
    ) -> None:
        self._run_id = run_id
        self._progress = progress
        self._cost_eur = cost_eur
        self._budget_eur = budget_eur
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._limit = limit
        self._model = model
        self._rubric_version = rubric_version
        self._max_per_company = max_per_company
        # Scores produced under different preferences are stale, not merely older: the
        # digest shows what the preferences in force actually decided.
        self._preferences_hash = preferences_hash

    def execute(self) -> str:
        self._publish(0)
        body = render_digest(
            self._store.match_scores(
                limit=self._limit,
                model=self._model,
                rubric_version=self._rubric_version,
                max_per_company=self._max_per_company,
                preferences_hash=self._preferences_hash,
            ),
            self._store.list_jobs(FilterOutcome.MANUAL_REVIEW, limit=self._limit),
            self._clock(),
            self._store.digest_stats(
                model=self._model,
                rubric_version=self._rubric_version,
                preferences_hash=self._preferences_hash,
            ),
            max_per_company=self._max_per_company,
            limit=self._limit,
        )
        self._notifier.deliver(TITLE, body)
        self._publish(1)
        return body

    def _publish(self, completed: int) -> None:
        if self._run_id is not None:
            progress = Progress("digest", completed, 1, self._cost_eur, self._budget_eur)
            self._store.update_run_progress(self._run_id, progress, self._clock())
            if self._progress:
                self._progress(progress)
