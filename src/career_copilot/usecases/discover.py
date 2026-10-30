"""Discovery run: fetch, normalize, filter, store.

Cheap tiers first, stop once the day's target of new qualified jobs is met.
A dead source is recorded and skipped, never fatal to the run.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from threading import Event

from career_copilot.domain.filters import FilterConfig, apply_hard_filters
from career_copilot.domain.models import (
    DiscoverReport,
    FilterOutcome,
    SourceRunReport,
    SourceTarget,
    UpsertOutcome,
)
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.runs import Progress, RunCancelled
from career_copilot.domain.source_health import (
    FailureRead,
    HealthConfig,
    blocks,
    record,
    unread,
)
from career_copilot.ports import JobSource, StateStore

log = logging.getLogger(__name__)


def main_reason(reasons: Sequence[str]) -> str:
    """The first rejection reason without its detail ("location outside Germany: Geneva"
    becomes "location outside Germany"), so reasons group across jobs."""
    first = reasons[0] if reasons else "no reason recorded"
    return re.split(r"[:(]", first)[0].strip()


def visit_key(tier: int, last_visited: datetime | None) -> tuple[int, float]:
    """Sort key for the order boards are visited in: tier first, then longest-waiting,
    never-visited ahead of all."""
    return tier, float("-inf") if last_visited is None else last_visited.timestamp()


class DiscoverRun:
    """Use case. Deterministic: no model call anywhere in this path."""

    def __init__(
        self,
        sources: dict[str, JobSource],
        store: StateStore,
        targets: Sequence[SourceTarget],
        clock: Callable[[], datetime],
        filter_config: FilterConfig | None = None,
        stop_after: int = 6,
        max_jobs_per_target: int = 100,
        run_id: int | None = None,
        progress: Callable[[Progress], None] | None = None,
        cancel: Event | None = None,
        budget_eur: float = 0,
        hide_reposts: Callable[[Sequence[int], datetime], int] | None = None,
        health: HealthConfig | None = None,
        read_failure: Callable[[Exception], FailureRead] = unread,
    ) -> None:
        self._health = health or HealthConfig()
        self._read_failure = read_failure
        self._hide_reposts = hide_reposts
        self._run_id = run_id
        self._progress = progress
        self._cancel = cancel
        self._budget_eur = budget_eur
        self._sources = sources
        self._store = store
        self._targets = targets
        self._clock = clock
        self._filters = filter_config or FilterConfig()
        self._stop_after = stop_after
        self._max_jobs_per_target = max_jobs_per_target

    def execute(self) -> DiscoverReport:
        now = self._clock()
        run_id = self._run_id if self._run_id is not None else self._store.start_run(now)
        aliases = self._store.company_aliases()

        fetched = inserted = changed = unchanged = 0
        new_ids: list[int] = []
        qualified = rejected = review = 0
        skipped: list[str] = []
        source_runs: list[SourceRunReport] = []
        reject_reasons: Counter[str] = Counter()

        # Targets are the only denominator discovery knows up front: the job count is
        # whatever the boards happen to return.
        targets = self._in_visit_order()
        self._publish(run_id, 0, len(targets))
        for done, target in enumerate(targets):
            self._check_cancel()
            self._publish(run_id, done, len(targets))
            if qualified >= self._stop_after:
                # Enough new matches for today. Tier-independent: the ladder decides the
                # *order* boards are drained in, not whether the day's target counts. The
                # condition used to require `tier > 0`, and every configured target is
                # tier 0, so the target could never be met and all 8 boards drained on
                # every run.
                skipped.append(f"{target.source}:{target.identifier} (target met)")
                continue

            source = self._sources.get(target.source)
            if source is None:
                skipped.append(f"{target.source}:{target.identifier} (no adapter)")
                continue

            health = self._store.source_health(target.source)
            if blocks(health, now):
                # The whole source is paused. Once the pause ends, the next target of it
                # is the retry, and its result decides whether the rest follow.
                until = health.paused_until.isoformat(timespec="minutes")  # type: ignore[union-attr]
                skipped.append(f"{target.source}:{target.identifier} (source paused until {until})")
                continue

            if not self._store.is_cooled_down(target.source, target.identifier, now):
                skipped.append(f"{target.source}:{target.identifier} (cooldown)")
                continue

            try:
                # Bounded per target, so one large board cannot be the whole run: a
                # single Greenhouse board contributed 361 of 1071 stored rows.
                raw_jobs = list(source.fetch(target))[: self._max_jobs_per_target]
            except Exception as exc:  # one broken source must not end the run
                log.warning("source failed: %s/%s: %s", target.source, target.identifier, exc)
                self._store.record_source_run(
                    run_id, target.source, target.identifier, False, 0, str(exc), now
                )
                failure = self._read_failure(exc)
                if failure.counts:
                    self._store.save_source_health(
                        record(
                            health,
                            target.source,
                            False,
                            now,
                            self._health,
                            reason=failure.reason,
                            message=str(exc),
                            retry_after_hours=failure.retry_after_hours,
                        )
                    )
                self._store.set_cooldown(
                    target.source, target.identifier, now, target.cooldown_hours, failed=True
                )
                source_runs.append(
                    SourceRunReport(target.source, target.identifier, ok=False, error=str(exc))
                )
                continue

            for raw in raw_jobs:
                self._check_cancel()
                fetched += 1
                posting = to_posting(raw, aliases)
                verdict = apply_hard_filters(posting, self._filters)
                job_id, outcome = self._store.upsert_job(posting, verdict, now)
                # Per-job, so the heartbeat gap stays bounded by one fetch.
                self._publish(run_id, done, len(targets))

                if outcome is UpsertOutcome.INSERTED:
                    inserted += 1
                    new_ids.append(job_id)
                elif outcome is UpsertOutcome.CHANGED:
                    changed += 1
                else:
                    unchanged += 1

                if verdict.outcome is FilterOutcome.REJECT:
                    rejected += 1
                    reject_reasons[main_reason(verdict.reasons)] += 1
                elif verdict.outcome is FilterOutcome.MANUAL_REVIEW:
                    review += 1
                elif outcome is UpsertOutcome.INSERTED:
                    qualified += 1

            self._store.record_source_run(
                run_id, target.source, target.identifier, True, len(raw_jobs), None, now
            )
            self._store.save_source_health(record(health, target.source, True, now, self._health))
            self._store.set_cooldown(target.source, target.identifier, now, target.cooldown_hours)
            source_runs.append(
                SourceRunReport(target.source, target.identifier, ok=True, fetched=len(raw_jobs))
            )

        if self._hide_reposts is not None and new_ids:
            hidden = self._hide_reposts(new_ids, now)
            if hidden:
                log.info("hid %d repost(s) of dismissed jobs", hidden)

        report = DiscoverReport(
            run_id=run_id,
            fetched=fetched,
            inserted=inserted,
            changed=changed,
            unchanged=unchanged,
            qualified=qualified,
            rejected=rejected,
            manual_review=review,
            reject_reasons=tuple(reject_reasons.most_common()),
            skipped_targets=tuple(skipped),
            source_runs=tuple(source_runs),
        )
        self._publish(run_id, len(targets), len(targets))
        if self._run_id is None:
            self._store.finish_run(
                run_id,
                self._clock(),
                f"fetched={fetched} new={inserted} qualified={qualified} "
                f"rejected={rejected} review={review} failed={len(report.failed_sources)}",
            )
        return report

    def _in_visit_order(self) -> list[SourceTarget]:
        """Tier first, then the board that has waited longest, never-visited ahead of all.

        Sorting on tier alone left the same boards first on every run, so once `stop_after`
        was met the boards behind them were never reached. A visit sends a board to the back
        of its tier; ties (never visited) keep the order they were configured in, and the
        sort is stable, so the result is deterministic."""
        visited = self._store.last_scraped_by_target()
        return sorted(
            self._targets,
            key=lambda t: visit_key(t.tier, visited.get((t.source, t.identifier))),
        )

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise RunCancelled()

    def _publish(self, run_id: int, completed: int, total: int) -> None:
        if self._run_id is not None:
            progress = Progress("discover", completed, total, 0, self._budget_eur)
            self._store.update_run_progress(run_id, progress, self._clock())
            if self._progress:
                self._progress(progress)
