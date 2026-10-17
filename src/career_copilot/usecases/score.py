"""Scoring run: triage every survivor, deep-score the top N, record everything."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from threading import Event

from career_copilot.agents.match import MatchAgent, MatchResult
from career_copilot.agents.triage import TriageAgent, TriageResult
from career_copilot.domain.budget import BudgetExceeded, CostMeter
from career_copilot.domain.diversity import cap_per_company
from career_copilot.domain.models import JobPosting, ScoreReport, ScoreStage, StoredJob
from career_copilot.domain.runs import Progress, RunCancelled
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from career_copilot.ports import LLMCallError, SchemaError, StateStore

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScoreBudget:
    """How many match calls this run may make, and what it may spend."""

    target: int
    max_eur: float
    mode: str
    triage_reserve_eur: float = 0.02
    match_reserve_eur: float = 0.20
    # A match counts toward `target` only at or above this score. 0 counts every match, which
    # is the old "score the top N" behaviour.
    min_score: int = 0


# Upper bound on match calls as a multiple of the target, so "keep going until N clear the
# bar" cannot turn into scoring every candidate on a board that has few good roles. The
# euro budget is the other bound.
MATCH_CALL_FACTOR = 3
# Lower on the embedding path: its order is good enough that a third pass over weak
# candidates mostly buys match calls that fall under the bar.
EMBEDDING_MATCH_CALL_FACTOR = 2

# Orders the candidates for the match step, or returns None when it cannot (no
# references, a broken embedder), in which case the run triages with the LLM instead.
Ranker = Callable[[Sequence[StoredJob], int], Sequence[StoredJob] | None]


def resolve_budget(
    has_match_scores: bool,
    initial_target: int,
    incremental_target: int,
    max_eur: float,
    force_initial: bool = False,
    override: int | None = None,
    triage_reserve_eur: float = 0.02,
    match_reserve_eur: float = 0.20,
) -> ScoreBudget:
    """An explicit override wins, then --initial, then auto-detection.

    Auto-detection keys off match scores only. Triage rows appear during the first
    run, so counting them would flip the mode mid-flight.
    """
    if override is not None:
        target, mode = override, "override"
    elif force_initial or not has_match_scores:
        target, mode = initial_target, "initial"
    else:
        target, mode = incremental_target, "incremental"
    return ScoreBudget(
        target=target,
        mode=mode,
        max_eur=max_eur,
        triage_reserve_eur=triage_reserve_eur,
        match_reserve_eur=match_reserve_eur,
    )


class ScoreRun:
    """Triage every survivor, deep-score the top N, record every result.

    Deterministic apart from the two model calls. Idempotent: the unique index on
    `scores` means a rerun over unchanged jobs does no work at all.
    """

    def __init__(
        self,
        store: StateStore,
        triage: TriageAgent,
        match: MatchAgent,
        clock: Callable[[], datetime],
        budget: ScoreBudget,
        rubric_version: int = RUBRIC_VERSION,
        candidate_limit: int | None = None,
        dry_run: bool = False,
        max_per_company: int = 2,
        preferences_version: int = 0,
        preferences_hash: str = "",
        cv_sha256: str | None = None,
        run_id: int | None = None,
        progress: Callable[[Progress], None] | None = None,
        cancel: Event | None = None,
        ranker: Ranker | None = None,
        match_concurrency: int = 1,
    ) -> None:
        self._ranker = ranker
        self._match_concurrency = match_concurrency
        self._run_id = run_id
        self._progress = progress
        self._cancel = cancel
        self._completed = 0
        self._total = 0
        self._failed = 0
        self._store = store
        self._triage = triage
        self._match = match
        self._clock = clock
        self._budget = budget
        self._rubric_version = rubric_version
        self._candidate_limit = candidate_limit
        self.dry_run = dry_run
        self._max_per_company = max_per_company
        # Stamped onto every score row and folded into the identity index, so an edit to a
        # prompt-affecting preference re-opens exactly the jobs whose result could differ.
        self._preferences_version = preferences_version
        self._preferences_hash = preferences_hash
        # Provenance stamp only; never part of the score identity.
        self._cv_sha256 = cv_sha256

    def execute(self) -> ScoreReport:
        now = self._clock()
        run_id = self._run_id if self._run_id is not None else self._store.start_run(now)
        meter = CostMeter()
        candidates = self._store.jobs_needing_score(
            self._match.model,
            self._rubric_version,
            self._candidate_limit,
            preferences_hash=self._preferences_hash,
        )

        if self.dry_run:
            # Estimated as the triage path: a dry run must not load an embedding model.
            estimated_calls = len(candidates) + min(len(candidates), self._budget.target)
            estimated_cost = (
                len(candidates) * self._budget.triage_reserve_eur
                + min(len(candidates), self._budget.target) * self._budget.match_reserve_eur
            )
            report = ScoreReport(
                run_id=run_id,
                mode=self._budget.mode,
                candidates=len(candidates),
                estimated_calls=estimated_calls,
                estimated_cost_eur=estimated_cost,
                cost_ceiling_eur=self._budget.max_eur,
            )
            self._finish(
                run_id,
                self._clock(),
                f"dry-run candidates={len(candidates)} estimated_calls={estimated_calls} "
                f"estimated_cost={estimated_cost:.4f} "
                f"cost_ceiling={self._budget.max_eur:.4f}",
            )
            return report

        ranked = self._ranker(candidates, self._budget.target) if self._ranker else None
        triaged: list[tuple[int, StoredJob]] = []
        if ranked is not None:
            ordered = list(ranked)
            factor, ranker_name = EMBEDDING_MATCH_CALL_FACTOR, "embedding"
        else:
            self._total = len(candidates)
            self._publish(run_id, meter, notify=False)
            try:
                for job in candidates:
                    try:
                        meter.reserve(self._budget.max_eur, self._budget.triage_reserve_eur)
                        result = self._evaluate(self._triage.evaluate, job, run_id, meter)
                    except SchemaError as exc:
                        # One malformed reply is a bad job, not a bad run. Skip it, count it,
                        # keep the remaining budget working.
                        meter.check(self._budget.max_eur)
                        log.warning("triage failed for job %d: %s", job.id, exc)
                        continue
                    except LLMCallError as exc:
                        if self._run_id is not None:
                            raise
                        return self._abort(
                            run_id,
                            meter,
                            str(exc),
                            len(candidates),
                            len(triaged),
                        )
                    self._store.record_score(
                        Score(
                            job_id=job.id,
                            stage=ScoreStage.TRIAGE,
                            score=result.relevance,
                            verdict=ScoreVerdict.from_score(result.relevance),
                            rubric_version=self._rubric_version,
                            model=result.model,
                            prompt_hash=result.prompt_hash,
                            content_hash=job.posting.content_hash,
                            reasons=(result.reason,) if result.reason else (),
                            preferences_version=self._preferences_version,
                            preferences_hash=self._preferences_hash,
                            cv_sha256=self._cv_sha256,
                        ),
                        now,
                        cost_eur=result.cost_eur,
                    )
                    meter.check(self._budget.max_eur)
                    triaged.append((result.relevance, job))
            except BudgetExceeded as exc:
                return self._abort(run_id, meter, str(exc), len(candidates), len(triaged))

            # Highest triage relevance first; ties break by id so the run is reproducible.
            ordered = [job for _, job in sorted(triaged, key=lambda t: (-t[0], t[1].id))]
            factor, ranker_name = MATCH_CALL_FACTOR, "llm_triage"
        pool = cap_per_company(
            ordered,
            len(ordered),
            self._max_per_company,
            company=lambda job: job.posting.company_norm,
        )
        capped = len(ordered) - len(pool)
        # Match in ranked order until `target` jobs clear the bar, not until `target` jobs
        # have been scored: a weak match does not count toward the day's quota.
        selected = pool[: self._budget.target * factor]

        self._total += len(selected)
        matched = 0
        above = 0
        workers = max(1, self._match_concurrency)
        # Only the model call runs on a worker. Metering, counters, progress and every store
        # write stay on this thread, so SQLite keeps its single writer and the meter needs no
        # lock. Results are consumed in rank order, so early stop is still best-first; calls
        # in flight when the target is reached finish and are recorded.
        pending: deque[tuple[StoredJob, Future[MatchResult | Exception]]] = deque()
        queue = iter(selected)
        exhausted = False
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="match")

        def stop(reason: str) -> ScoreReport:
            self._drain(pending, meter)
            return self._abort(run_id, meter, reason, len(candidates), len(triaged), matched)

        try:
            while True:
                try:
                    while not exhausted and len(pending) < workers and above < self._budget.target:
                        upcoming = next(queue, None)
                        if upcoming is None:
                            exhausted = True
                            break
                        job = upcoming
                        # Calls in flight are not metered yet, so reserve for all of them.
                        meter.reserve(
                            self._budget.max_eur,
                            self._budget.match_reserve_eur * (len(pending) + 1),
                        )
                        self._raise_if_cancelled()
                        pending.append(
                            (job, executor.submit(self._call, self._match.evaluate, job.posting))
                        )
                    if not pending:
                        break
                    job, future = pending.popleft()
                    match_result = self._settle(future.result(), run_id, meter)
                except BudgetExceeded as exc:
                    return stop(str(exc))
                except SchemaError as exc:
                    log.warning("scoring failed for job %d: %s", job.id, exc)
                    try:
                        meter.check(self._budget.max_eur)
                    except BudgetExceeded as budget_exc:
                        return stop(str(budget_exc))
                    continue
                except LLMCallError as exc:
                    if self._run_id is not None:
                        raise
                    return stop(str(exc))
                self._store.record_score(
                    Score(
                        job_id=job.id,
                        stage=ScoreStage.MATCH,
                        score=match_result.score,
                        verdict=match_result.verdict,
                        rubric_version=self._rubric_version,
                        model=match_result.model,
                        prompt_hash=match_result.prompt_hash,
                        content_hash=job.posting.content_hash,
                        reasons=match_result.reasons,
                        gaps=match_result.gaps,
                        warnings=match_result.warnings,
                        evidence_refs=match_result.evidence_refs,
                        parts={
                            name: {"points": part.points, "reason": part.reason}
                            for name, part in match_result.parts.items()
                        },
                        preferences_version=self._preferences_version,
                        preferences_hash=self._preferences_hash,
                        cv_sha256=self._cv_sha256,
                    ),
                    now,
                    cost_eur=match_result.cost_eur,
                )
                self._store.clear_needs_rescore(job.id)
                matched += 1
                if match_result.score >= self._budget.min_score:
                    above += 1
                try:
                    meter.check(self._budget.max_eur)
                except BudgetExceeded as exc:
                    return stop(str(exc))
        except BaseException:
            self._drain(pending, meter)
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        self._store.record_run_cost(run_id, meter.calls, meter.spent)
        self._finish(
            run_id,
            self._clock(),
            f"mode={self._budget.mode} ranker={ranker_name} triaged={len(triaged)} "
            f"matched={matched} "
            f"above={above}/{self._budget.target} capped={capped} failed={self._failed} "
            f"cost={meter.spent:.4f}",
        )
        return ScoreReport(
            run_id=run_id,
            mode=self._budget.mode,
            candidates=len(candidates),
            triaged=len(triaged),
            matched=matched,
            above_threshold=above,
            target=self._budget.target,
            capped=capped,
            failed=self._failed,
            llm_calls=meter.calls,
            cost_eur=meter.spent,
        )

    def _finish(self, run_id: int, now: datetime, summary: str) -> None:
        if self._run_id is None:
            self._store.finish_run(run_id, now, summary)

    def _publish(self, run_id: int, meter: CostMeter, *, notify: bool = True) -> None:
        if self._run_id is None:
            return
        progress = Progress(
            "score", self._completed, self._total, meter.spent, self._budget.max_eur, self._failed
        )
        self._store.update_run_progress(run_id, progress, self._clock())
        if notify and self._progress:
            self._progress(progress)

    def _raise_if_cancelled(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise RunCancelled()

    @staticmethod
    def _call[T: (TriageResult, MatchResult)](
        evaluate: Callable[[JobPosting], T], posting: JobPosting
    ) -> T | Exception:
        """The part that may run on a worker thread: the model call and nothing else."""
        try:
            return evaluate(posting)
        except Exception as exc:
            return exc

    def _settle[T: (TriageResult, MatchResult)](
        self, outcome: T | Exception, run_id: int, meter: CostMeter
    ) -> T:
        """Account for one finished call. Always on the run's own thread."""
        try:
            if isinstance(outcome, Exception):
                if isinstance(outcome, LLMCallError):
                    # The single accounting point: every call the adapter billed for is
                    # metered here, whether it produced a result or an error.
                    meter.add_usage(outcome.calls, outcome.cost_eur)
                    self._failed += 1
                raise outcome
            meter.add_usage(outcome.calls, outcome.cost_eur)
            return outcome
        finally:
            self._completed += 1
            if self._run_id is not None:
                self._store.record_run_cost(run_id, meter.calls, meter.spent)
                self._publish(run_id, meter)

    def _evaluate[T: (TriageResult, MatchResult)](
        self,
        evaluate: Callable[[JobPosting], T],
        job: StoredJob,
        run_id: int,
        meter: CostMeter,
    ) -> T:
        self._raise_if_cancelled()
        return self._settle(self._call(evaluate, job.posting), run_id, meter)

    @staticmethod
    def _drain(
        pending: deque[tuple[StoredJob, Future[MatchResult | Exception]]], meter: CostMeter
    ) -> None:
        """Calls already in flight were billed even if nobody will read them: meter them."""
        while pending:
            _, future = pending.popleft()
            if future.cancel():
                continue
            outcome = future.result()
            if isinstance(outcome, MatchResult | LLMCallError):
                meter.add_usage(outcome.calls, outcome.cost_eur)

    def _abort(
        self,
        run_id: int,
        meter: CostMeter,
        reason: str,
        candidates: int,
        triaged: int,
        matched: int = 0,
    ) -> ScoreReport:
        """Loud stop. The spend so far is recorded; nothing is silently continued."""
        if self._run_id is not None:
            raise BudgetExceeded(reason)
        log.error("scoring run aborted: %s", reason)
        self._store.record_run_cost(run_id, meter.calls, meter.spent)
        self._finish(run_id, self._clock(), f"aborted: {reason}")
        return ScoreReport(
            run_id=run_id,
            mode=self._budget.mode,
            candidates=candidates,
            triaged=triaged,
            matched=matched,
            failed=self._failed,
            llm_calls=meter.calls,
            cost_eur=meter.spent,
            aborted=True,
            abort_reason=reason,
        )
