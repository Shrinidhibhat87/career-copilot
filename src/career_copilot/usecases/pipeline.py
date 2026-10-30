"""Shared composition root for CLI stages and browser pipeline execution."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

from career_copilot.adapters.llm.backends import build_client
from career_copilot.adapters.llm.redacting_client import RedactingLLMClient
from career_copilot.adapters.notify.markdown import MarkdownNotifier
from career_copilot.adapters.observability import agent_recording, recorder_for
from career_copilot.adapters.pii.factory import build_redactor
from career_copilot.adapters.profile_loader import load_notes
from career_copilot.adapters.sources.ats import SOURCE_TYPES
from career_copilot.adapters.sources.failures import read_failure
from career_copilot.adapters.sources.jobsuche import SEARCH_SOURCES, JobsucheSource
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.curator.agent import build_curator
from career_copilot.agents.discovery.agent import build_model
from career_copilot.agents.match import MatchAgent
from career_copilot.agents.profiler import build_profiler
from career_copilot.agents.triage import TriageAgent
from career_copilot.config import Config
from career_copilot.domain.budget import BudgetExceeded
from career_copilot.domain.diversity import cap_per_company
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.preference_note import build_preference_note
from career_copilot.domain.preferences import Preferences, bootstrap
from career_copilot.domain.profile import Profile
from career_copilot.domain.runs import Progress, RunCancelled, RunConfig, ScorePreview
from career_copilot.domain.search_targets import search_targets
from career_copilot.domain.source_health import HealthConfig
from career_copilot.ports import (
    FAILURE_MESSAGES,
    Embedder,
    JobSource,
    LLMCallError,
    LLMClient,
    PreferencesStore,
    ResumeMissingError,
)
from career_copilot.usecases.curate import EmbeddingRanker
from career_copilot.usecases.cv import MISSING_MESSAGE, ensure_current_cv
from career_copilot.usecases.digest import DigestRun
from career_copilot.usecases.discover import DiscoverRun
from career_copilot.usecases.funnel import build_funnel
from career_copilot.usecases.preferences import load_preferences
from career_copilot.usecases.repost_guard import hide_reposts
from career_copilot.usecases.score import (
    EMBEDDING_MATCH_CALL_FACTOR,
    Ranker,
    ScoreBudget,
    ScoreRun,
    resolve_budget,
)
from career_copilot.usecases.search_profile import ensure_profile

log = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(UTC)


def config_seed(config: Config) -> Preferences:
    return bootstrap(
        config.filters.to_filter_config(),
        notes=load_notes(config.profile_dir),
        batch_size=config.scoring.incremental_target,
    )


def current_preferences(store: PreferencesStore, config: Config) -> Preferences:
    return load_preferences(store, config_seed(config))


def with_profile(prefs: Preferences, store: SqliteStore) -> Preferences:
    """The preferences with the newest search profile attached, as a run will see them."""
    profile = store.latest_search_profile()
    return replace(prefs, search_profile=profile.to_prompt_text() if profile else "")


def freeze_config(
    config: Config,
    store: SqliteStore,
    prefs: Preferences,
    *,
    initial: bool = False,
    budget_override: int | None = None,
) -> RunConfig:
    scoring = config.scoring
    prefs = with_profile(prefs, store)
    # Roles, not `scoring:` ids: the frozen run must name the model that will actually
    # answer, so a score recorded against a local model is never mistaken for a Gemini
    # one when the two are swapped in config.
    triage_role = config.role("triage")
    match_role = config.role("match")
    budget = resolve_budget(
        store.has_match_scores(),
        scoring.initial_target,
        prefs.batch_size,
        scoring.max_eur_per_run,
        initial,
        budget_override,
        triage_role.reserve_eur,
        match_role.reserve_eur,
    )
    return RunConfig(
        prefs,
        triage_role.litellm_model,
        match_role.litellm_model,
        scoring.rubric_version,
        budget.mode,
        budget.max_eur,
        budget.target,
        budget.triage_reserve_eur,
        budget.match_reserve_eur,
        triage_role.max_output_tokens,
        match_role.max_output_tokens,
    )


def preview_score(
    config: Config,
    store: SqliteStore,
    prefs: Preferences,
    *,
    initial: bool = False,
    budget_override: int | None = None,
) -> ScorePreview:
    frozen = freeze_config(config, store, prefs, initial=initial, budget_override=budget_override)
    candidates = store.jobs_needing_score(
        frozen.match_model, frozen.rubric_version, preferences_hash=frozen.preferences_hash
    )
    selected = cap_per_company(
        candidates,
        frozen.target,
        prefs.max_per_company,
        company=lambda job: job.posting.company_norm,
    )
    if config.scoring.ranker == "embedding" and has_references(store, frozen):
        # No triage calls: ranking is local. The match step may go to the call factor.
        calls = min(len(candidates), frozen.target * EMBEDDING_MATCH_CALL_FACTOR)
        return ScorePreview(
            len(candidates),
            calls,
            calls * frozen.match_reserve_eur,
            frozen.budget_eur,
            frozen.mode,
            frozen.token(),
        )
    return ScorePreview(
        len(candidates),
        len(candidates) + len(selected),
        len(candidates) * frozen.triage_reserve_eur + len(selected) * frozen.match_reserve_eur,
        frozen.budget_eur,
        frozen.mode,
        frozen.token(),
    )


def has_references(store: SqliteStore, frozen: RunConfig) -> bool:
    """Whether the embedding ranker has anything to rank against."""
    return bool(
        frozen.preferences.search_profile.strip()
        or store.active_intent()
        or store.active_sample_roles()
    )


def crawl_targets(
    config: Config, store: SqliteStore, preferences: Preferences | None = None
) -> list[SourceTarget]:
    """The hand-written seed plus every board the user approved, then the searches the
    saved preferences imply (given `preferences`, and unless switched off in config).

    `config.yaml` stays the user's own list and is never rewritten by a run; approved
    proposals live in the store. Order is seed first, so an approved board cannot push
    a configured one out of a bounded run.
    """
    targets = list(config.source_targets())
    seen = {(target.source, target.identifier) for target in targets}
    for target in store.list_source_targets():
        if (target.source, target.identifier) not in seen:
            targets.append(target)
    if preferences is not None and config.jobsuche.enabled:
        targets.extend(
            search_targets(JobsucheSource.name, preferences, radius_km=config.jobsuche.radius_km)
        )
    return targets


def build_sources() -> dict[str, JobSource]:
    """Every adapter a run can use: company boards first, then the searched sources."""
    sources: dict[str, JobSource] = {name: cls() for name, cls in SOURCE_TYPES.items()}
    sources.update({name: cls() for name, cls in SEARCH_SOURCES.items()})
    return sources


def build_discover_run(
    config: Config,
    store: SqliteStore,
    run_id: int,
    *,
    progress: Callable[[Progress], None] | None = None,
    cancel: Event | None = None,
) -> DiscoverRun:
    frozen = store.run_config(run_id)

    def embedder() -> Embedder:
        # Deferred: the ONNX model only loads when a new job shares a company with a
        # dismissed one.
        from career_copilot.adapters.embed.fastembed_embedder import FastEmbedEmbedder

        return FastEmbedEmbedder(config.scoring.embedding_model)

    return DiscoverRun(
        sources=build_sources(),
        store=store,
        targets=crawl_targets(config, store, frozen.preferences),
        clock=now,
        filter_config=frozen.preferences.to_filter_config(),
        stop_after=config.stop_after if store.has_jobs() else config.initial_target,
        max_jobs_per_target=config.max_jobs_per_target,
        run_id=run_id,
        progress=progress,
        cancel=cancel,
        budget_eur=frozen.budget_eur,
        health=HealthConfig(**config.source_health.model_dump()),
        read_failure=read_failure,
        hide_reposts=lambda ids, at: hide_reposts(
            store, embedder, ids, config.dedupe.repost_similarity, at
        ),
    )


def refresh_profile(
    config: Config, store: SqliteStore, frozen: RunConfig, run_id: int | None = None
) -> RunConfig:
    """Rebuild the profile if its inputs changed since the run was frozen.

    The profile changes the prompt, so it changes the preferences hash; scoring and the
    digest both read the returned config, so they agree on which scores are current.
    """
    role = config.role("profiler")
    cv = store.current_cv()

    def redact(texts: Sequence[str]) -> Sequence[str]:
        # Built only when a profile is actually due: loading Presidio is not free.
        redactor = build_redactor(config, skill_terms=frozen.preferences.core_skills)
        if redactor is None:
            return texts
        if cv is not None:
            redactor.learn(cv.text)
        return redactor.redact(texts).texts

    profile = ensure_profile(
        store,
        lambda: build_profiler(build_model(role)),
        cv,
        redact,
        role.litellm_model,
        now(),
        recording=agent_recording(config, role, "profiler", run_id),
    )
    text = profile.to_prompt_text() if profile else ""
    if text == frozen.preferences.search_profile:
        return frozen
    return replace(frozen, preferences=replace(frozen.preferences, search_profile=text))


def build_ranker(
    config: Config, store: SqliteStore, frozen: RunConfig, run_id: int | None = None
) -> Ranker | None:
    """The embedding ranker with the curator on top, or None for the LLM triage path."""
    scoring = config.scoring
    if scoring.ranker != "embedding":
        return None

    def embedder() -> Embedder:
        # Deferred: loading the ONNX model is only worth it once there is something to rank.
        from career_copilot.adapters.embed.fastembed_embedder import FastEmbedEmbedder

        return FastEmbedEmbedder(scoring.embedding_model)

    role = config.role("curator")
    recording = agent_recording(config, role, "curator", run_id)
    return EmbeddingRanker(
        store=store,
        embedder=embedder,
        clock=now,
        profile=frozen.preferences.search_profile,
        cv_text=cv.text if (cv := store.current_cv()) is not None else "",
        cv_weight=scoring.cv_weight,
        curator=(lambda: build_curator(build_model(role), recording)) if scoring.curate else None,
        recording=recording,
    )


def build_score_run(
    config: Config,
    store: SqliteStore,
    run_id: int,
    frozen: RunConfig,
    *,
    progress: Callable[[Progress], None] | None = None,
    cancel: Event | None = None,
) -> ScoreRun:
    prefs = frozen.preferences
    recorder = recorder_for(config, run_id)
    cv = ensure_current_cv(store)
    resume = cv.text
    profile = Profile(preferences=prefs.to_prompt_block(), resume=resume)
    redactor = build_redactor(config, skill_terms=prefs.core_skills)
    if redactor is not None:
        redactor.learn(resume)

    def with_redaction(client: LLMClient) -> LLMClient:
        return (
            client
            if redactor is None
            else RedactingLLMClient(client, redactor, outbound=config.pii.outbound)
        )

    return ScoreRun(
        store=store,
        triage=TriageAgent(
            with_redaction(build_client(config.role("triage"), recorder, run_id)),
            profile.preferences,
        ),
        match=MatchAgent(
            with_redaction(build_client(config.role("match"), recorder, run_id)),
            profile,
            learned=build_preference_note(store.decision_signals()),
        ),
        clock=now,
        budget=ScoreBudget(
            frozen.target,
            frozen.budget_eur,
            frozen.mode,
            frozen.triage_reserve_eur,
            frozen.match_reserve_eur,
            min_score=config.scoring.min_match_score,
        ),
        rubric_version=frozen.rubric_version,
        max_per_company=prefs.max_per_company,
        preferences_version=frozen.preferences_version,
        preferences_hash=frozen.preferences_hash,
        cv_sha256=cv.sha256,
        run_id=run_id,
        progress=progress,
        cancel=cancel,
        ranker=build_ranker(config, store, frozen, run_id),
        match_concurrency=config.role("match").max_concurrency,
    )


def build_digest_run(
    config: Config,
    store: SqliteStore,
    run_id: int | None,
    frozen: RunConfig,
    *,
    out: str | None = None,
    limit: int = 20,
    progress: Callable[[Progress], None] | None = None,
) -> DigestRun:
    # `run_id` is None for a standalone digest: it renders what is already scored and
    # takes no run slot.
    row = store.get_run(run_id) if run_id is not None else None
    return DigestRun(
        store=store,
        notifier=MarkdownNotifier(out or config.scoring.digest_path),
        clock=now,
        limit=limit,
        model=frozen.match_model,
        rubric_version=frozen.rubric_version,
        max_per_company=frozen.preferences.max_per_company,
        preferences_hash=frozen.preferences_hash,
        run_id=run_id,
        progress=progress,
        cost_eur=float(row["cost_eur"]) if row else 0,
        budget_eur=frozen.budget_eur,
    )


def finalize_error(store: SqliteStore, run_id: int, exc: BaseException) -> None:
    if isinstance(exc, RunCancelled | KeyboardInterrupt):
        store.abort_run(run_id, now(), "shutdown")
    elif isinstance(exc, BudgetExceeded):
        store.abort_run(run_id, now(), str(exc))
    elif isinstance(exc, ResumeMissingError):
        store.fail_run(run_id, now(), "resume_missing", MISSING_MESSAGE)
    elif isinstance(exc, LLMCallError):
        store.fail_run(run_id, now(), exc.category, FAILURE_MESSAGES[exc.category])
    else:
        store.fail_run(
            run_id, now(), "internal", "The run stopped unexpectedly. Check the local setup."
        )


class PipelineRun:
    """Execute against an existing row; this class never acquires the run slot."""

    def __init__(
        self,
        store: SqliteStore,
        config: Config,
        run_id: int,
        *,
        cancel: Event | None = None,
        progress: Callable[[Progress], None] | None = None,
    ) -> None:
        self.store, self.config, self.run_id = store, config, run_id
        self.cancel = cancel or Event()
        self.progress = progress

    def execute(self) -> None:
        try:
            frozen = self.store.run_config(self.run_id)
            self._check_cancel()
            with self._stage("discover"):
                discovered = build_discover_run(
                    self.config, self.store, self.run_id, cancel=self.cancel, progress=self.progress
                ).execute()
                self.store.mark_inactive_before(
                    now() - timedelta(days=self.config.inactive_after_days)
                )
                log.info(
                    "run %d discover: %d targets searched, %d skipped, %d failed; "
                    "%d fetched, %d new, %d qualified, %d rejected",
                    self.run_id,
                    len(discovered.source_runs),
                    len(discovered.skipped_targets),
                    len(discovered.failed_sources),
                    discovered.fetched,
                    discovered.inserted,
                    discovered.qualified,
                    discovered.rejected,
                )
            self._check_cancel()
            with self._stage("profile"):
                frozen = refresh_profile(self.config, self.store, frozen, self.run_id)
            self._check_cancel()
            with self._stage("score"):
                scored = build_score_run(
                    self.config,
                    self.store,
                    self.run_id,
                    frozen,
                    cancel=self.cancel,
                    progress=self.progress,
                ).execute()
            funnel = build_funnel(discovered, scored, self.config.scoring.min_match_score)
            self.store.record_funnel(self.run_id, funnel)
            log.info(
                "run %d score: %d candidates, %d capped, %d matched, %d at or above %d "
                "(target %d); stopped by: %s",
                self.run_id,
                scored.candidates,
                scored.capped,
                scored.matched,
                scored.above_threshold,
                self.config.scoring.min_match_score,
                scored.target,
                funnel["score"]["stoppedBy"],
            )
            self._check_cancel()
            with self._stage("digest"):
                build_digest_run(
                    self.config, self.store, self.run_id, frozen, progress=self.progress
                ).execute()
            # The `key=value` tail is read by the chat to say what the run did and which
            # limit stopped it.
            self.store.finish_run(
                self.run_id,
                now(),
                f"discover → score → digest | candidates={scored.candidates} "
                f"matched={scored.matched} above={scored.above_threshold}/{scored.target} "
                f"capped={scored.capped}",
            )
        except BaseException as exc:
            finalize_error(self.store, self.run_id, exc)
            # A terminal row is not a reason to swallow the interpreter's own exits: the
            # CLI still has to stop, and Ctrl-C still has to look like Ctrl-C.
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
        finally:
            # Conditional transition: does nothing after any successful finalization.
            self.store.fail_run(self.run_id, now(), "internal", "The run stopped unexpectedly.")

    @contextmanager
    def _stage(self, name: str) -> Iterator[None]:
        """Records the stage's start and end. A stage that raises keeps its end open."""
        self.store.start_stage(self.run_id, name, now())
        started = now()
        yield
        self.store.end_stage(self.run_id, name, now())
        log.info(
            "run %d stage %s done in %.1fs", self.run_id, name, (now() - started).total_seconds()
        )

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise RunCancelled()
