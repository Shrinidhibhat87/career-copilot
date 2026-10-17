"""Domain entities. Frozen dataclasses, stdlib only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class FilterOutcome(StrEnum):
    """Result of the deterministic hard-filter pass."""

    PASS = "pass"
    MANUAL_REVIEW = "manual_review"
    REJECT = "reject"


class JobStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class UpsertOutcome(StrEnum):
    """What an upsert did, so callers know whether work is needed."""

    INSERTED = "inserted"
    UNCHANGED = "unchanged"
    CHANGED = "changed"


class ScoreStage(StrEnum):
    """Which pass produced a score. Triage is cheap and wide; match is deep."""

    TRIAGE = "triage"
    MATCH = "match"


@dataclass(frozen=True, slots=True)
class RawJob:
    """What a JobSource returns, before normalization."""

    source: str
    source_job_id: str
    company: str
    title: str
    location: str
    url: str
    description: str
    posted_at: datetime | None = None
    employment_type: str | None = None
    remote: bool | None = None


@dataclass(frozen=True, slots=True)
class JobPosting:
    """A normalized job with its dedupe identity."""

    source: str
    source_job_id: str
    company: str
    company_norm: str
    title: str
    title_norm: str
    location: str
    location_norm: str
    canonical_url: str
    description: str
    fingerprint: str
    content_hash: str
    posted_at: datetime | None = None
    employment_type: str | None = None
    remote: bool | None = None


@dataclass(frozen=True, slots=True)
class HardFilterResult:
    """Why a job was kept, parked for review, or dropped."""

    outcome: FilterOutcome
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_rejected(self) -> bool:
        return self.outcome is FilterOutcome.REJECT


@dataclass(frozen=True, slots=True)
class StoredJob:
    """A job as held by the store, with observation history."""

    id: int
    posting: JobPosting
    filter_outcome: FilterOutcome
    filter_reasons: tuple[str, ...]
    filter_warnings: tuple[str, ...]
    status: JobStatus
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class SourceTarget:
    """One fetchable unit: a company board or a query, at a ladder tier."""

    source: str
    identifier: str
    tier: int = 0
    cooldown_hours: int = 24


@dataclass(frozen=True, slots=True)
class SourceRunReport:
    """Outcome of one target fetch. A quiet failure must never read as zero jobs."""

    source: str
    identifier: str
    ok: bool
    fetched: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoverReport:
    """Outcome of a full discovery run."""

    run_id: int
    fetched: int = 0
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0
    qualified: int = 0
    rejected: int = 0
    manual_review: int = 0
    # Why jobs were rejected, as (main reason, count), most common first.
    reject_reasons: tuple[tuple[str, int], ...] = ()
    skipped_targets: tuple[str, ...] = ()
    source_runs: tuple[SourceRunReport, ...] = field(default_factory=tuple)

    @property
    def failed_sources(self) -> tuple[str, ...]:
        return tuple(f"{r.source}:{r.identifier}" for r in self.source_runs if not r.ok)


@dataclass(frozen=True, slots=True)
class ScoreReport:
    """Outcome of a scoring run."""

    run_id: int
    mode: str = ""
    candidates: int = 0
    triaged: int = 0
    matched: int = 0
    # Matches at or above `scoring.min_match_score`, against the run's `target`: the run's
    # honest answer to "did it find enough".
    above_threshold: int = 0
    target: int = 0
    # Ranked candidates the per-company cap left out of this run's match pool.
    capped: int = 0
    failed: int = 0
    llm_calls: int = 0
    cost_eur: float = 0.0
    aborted: bool = False
    abort_reason: str = ""
    estimated_calls: int = 0
    estimated_cost_eur: float = 0.0
    cost_ceiling_eur: float = 0.0


@dataclass(frozen=True, slots=True)
class DigestStats:
    """Current-state statistics and the latest discovery failures."""

    active_jobs: int = 0
    scored_jobs: int = 0
    manual_review: int = 0
    failed_sources: tuple[str, ...] = ()
    # A curated watchlist's current coverage, read live (not sync-time) so it never goes
    # stale between syncs. Kept as two distinct tuples, not one: "no public board found"
    # and "a board may exist but couldn't be verified" are different signals to a reader.
    watchlist_unresolved: tuple[str, ...] = ()
    watchlist_blocked: tuple[tuple[str, str], ...] = ()
