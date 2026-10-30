"""What the Settings page and the CLI show about each job source, and the test they offer.

The test fetches with the real adapter and writes nothing: no jobs, no cooldown, no breaker
state. It reports a reason in words, never the raw error, which can carry a URL.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from career_copilot.adapters.sources.failures import read_failure
from career_copilot.adapters.sources.jobsuche import SEARCH_SOURCES
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.source_health import (
    SourceHealth,
    blocks,
    explain,
    resume,
    status,
)
from career_copilot.ports import JobSource
from career_copilot.usecases.pipeline import crawl_targets, current_preferences

FETCH_WINDOW_DAYS = 7


class UnknownSource(KeyError):
    """No adapter has this name."""


@dataclass(frozen=True, slots=True)
class SourceOverview:
    source: str
    status: str
    targets: int
    last_ok_at: datetime | None
    last_failure: str | None
    last_message: str | None
    paused_until: datetime | None
    fetched_last_7_days: int


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    fetched: int = 0
    duration_ms: int = 0
    reason: str | None = None
    message: str | None = None


def source_overview(
    store: SqliteStore, config: Config, sources: dict[str, JobSource], now: datetime
) -> list[SourceOverview]:
    """Every source with a board to visit or a history, most trouble first."""
    targets = crawl_targets(config, store, current_preferences(store, config))
    counts: dict[str, int] = {}
    for target in targets:
        counts[target.source] = counts.get(target.source, 0) + 1
    health = store.list_source_health()
    fetched = store.fetched_since(now - timedelta(days=FETCH_WINDOW_DAYS))
    rows = [
        _overview(name, counts.get(name, 0), health.get(name), fetched.get(name, 0), now)
        for name in sources
        if counts.get(name) or name in health
    ]
    order = {"paused": 0, "degraded": 1, "unknown": 2, "ok": 3}
    return sorted(rows, key=lambda row: (order[row.status], row.source))


def _overview(
    name: str, targets: int, health: SourceHealth | None, fetched: int, now: datetime
) -> SourceOverview:
    reason = health.last_failure.value if health and health.last_failure else None
    return SourceOverview(
        source=name,
        status=status(health, now),
        targets=targets,
        last_ok_at=health.last_ok_at if health else None,
        last_failure=reason,
        last_message=explain(reason),
        paused_until=health.paused_until if health and blocks(health, now) else None,
        fetched_last_7_days=fetched,
    )


def try_source(
    store: SqliteStore,
    config: Config,
    sources: dict[str, JobSource],
    name: str,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeResult:
    """One real request for the source. Writes nothing."""
    source = sources.get(name)
    if source is None:
        raise UnknownSource(name)
    started = clock()
    try:
        if name in SEARCH_SOURCES:
            fetched = source.ping()  # type: ignore[attr-defined]
        else:
            target = _first_target(store, config, name)
            if target is None:
                return ProbeResult(ok=False, message="no board of this source is set up")
            fetched = len(list(source.fetch(target)))
    except Exception as error:
        reason = read_failure(error).reason.value
        return ProbeResult(
            ok=False, duration_ms=_millis(started, clock), reason=reason, message=explain(reason)
        )
    return ProbeResult(ok=True, fetched=fetched, duration_ms=_millis(started, clock))


def resume_source(
    store: SqliteStore, sources: dict[str, JobSource], name: str, now: datetime
) -> str:
    """Cut a pause short so the next run retries the source. Returns its status."""
    if name not in sources:
        raise UnknownSource(name)
    health = store.source_health(name)
    resumed = resume(health, now)
    if resumed is not None and resumed is not health:
        store.save_source_health(resumed)
    return status(resumed, now)


def sources_to_test(
    store: SqliteStore, config: Config, sources: dict[str, JobSource]
) -> Sequence[str]:
    """The sources a bare `sources test` covers: those with a board to visit."""
    targets = crawl_targets(config, store, current_preferences(store, config))
    wanted = {target.source for target in targets}
    return [name for name in sources if name in wanted]


def _first_target(store: SqliteStore, config: Config, name: str) -> SourceTarget | None:
    for target in crawl_targets(config, store, current_preferences(store, config)):
        if target.source == name:
            return target
    return None


def _millis(started: float, clock: Callable[[], float]) -> int:
    return round((clock() - started) * 1000)
