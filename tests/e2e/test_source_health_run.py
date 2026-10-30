"""The per-source breaker inside a discovery run, on fakes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from career_copilot.adapters.sources.ats import BoardNotFound
from career_copilot.adapters.sources.failures import read_failure
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.models import DiscoverReport, RawJob, SourceTarget
from career_copilot.domain.source_health import FailureReason, HealthConfig
from career_copilot.usecases.discover import DiscoverRun
from tests.conftest import FakeSource, make_raw

T0 = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as s:
        yield s


class Flaky:
    """Fails while `down`, with the error it was given."""

    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self.down = True
        self.error = error or httpx.ConnectError("refused")
        self.calls = 0

    def fetch(self, target: SourceTarget) -> list[RawJob]:
        self.calls += 1
        if self.down:
            raise self.error
        return [
            make_raw(source_job_id=target.identifier, url=f"https://x.test/{target.identifier}")
        ]


def run(
    store: SqliteStore, sources: dict[str, Any], targets: list[SourceTarget], at: datetime
) -> DiscoverReport:
    return DiscoverRun(
        sources,
        store,
        targets,
        lambda: at,
        health=HealthConfig(open_after=3, pause_hours=24, max_pause_hours=168),
        read_failure=read_failure,
        stop_after=999,
    ).execute()


def boards(source: str, count: int) -> list[SourceTarget]:
    return [SourceTarget(source, f"b{i}", cooldown_hours=1) for i in range(count)]


def test_three_failing_boards_pause_the_fourth_and_the_skip_is_listed(store: SqliteStore) -> None:
    flaky = Flaky("workable")
    report = run(store, {"workable": flaky}, boards("workable", 5), T0)
    assert flaky.calls == 3
    assert report.failed_sources == ("workable:b0", "workable:b1", "workable:b2")
    assert [s for s in report.skipped_targets if "source paused" in s] == [
        f"workable:b{i} (source paused until 2026-08-13T09:00+00:00)" for i in (3, 4)
    ]
    health = store.source_health("workable")
    assert health is not None and health.last_failure is FailureReason.UNREACHABLE


def test_another_source_is_unaffected(store: SqliteStore) -> None:
    flaky, fine = Flaky("workable"), Flaky("lever")
    fine.down = False
    targets = [*boards("workable", 3), SourceTarget("lever", "ok")]
    report = run(store, {"workable": flaky, "lever": fine}, targets, T0)
    assert report.inserted == 1 and fine.calls == 1


def test_after_the_pause_one_board_is_the_retry_and_a_success_reopens_the_rest(
    store: SqliteStore,
) -> None:
    flaky = Flaky("workable")
    run(store, {"workable": flaky}, boards("workable", 3), T0)
    flaky.down, flaky.calls = False, 0
    later = T0 + timedelta(hours=25)
    report = run(store, {"workable": flaky}, boards("workable", 3), later)
    assert flaky.calls == 3 and report.fetched == 3
    health = store.source_health("workable")
    assert health is not None and health.consecutive_failures == 0 and health.paused_until is None


def test_a_failed_retry_pauses_again_for_longer_and_tries_only_one_board(
    store: SqliteStore,
) -> None:
    flaky = Flaky("workable")
    run(store, {"workable": flaky}, boards("workable", 3), T0)
    flaky.calls = 0
    later = T0 + timedelta(hours=25)
    report = run(store, {"workable": flaky}, boards("workable", 3), later)
    assert flaky.calls == 1
    assert len([s for s in report.skipped_targets if "source paused" in s]) == 2
    health = store.source_health("workable")
    assert health is not None and health.pause_hours == 48
    assert health.paused_until == later + timedelta(hours=48)


def test_a_missing_board_never_pauses_the_source(store: SqliteStore) -> None:
    gone = Flaky("greenhouse", BoardNotFound("no such board"))
    run(store, {"greenhouse": gone}, boards("greenhouse", 5), T0)
    assert gone.calls == 5
    health = store.source_health("greenhouse")
    assert health is None or health.consecutive_failures == 0


def test_a_long_retry_after_pauses_at_once_and_is_remembered_across_runs(
    store: SqliteStore,
) -> None:
    request = httpx.Request("GET", "https://apply.workable.com/x")
    banned = httpx.HTTPStatusError(
        "429",
        request=request,
        response=httpx.Response(429, headers={"retry-after": "86400"}, request=request),
    )
    flaky = Flaky("workable", banned)
    run(store, {"workable": flaky}, boards("workable", 4), T0)
    assert flaky.calls == 1
    flaky.calls = 0
    run(store, {"workable": flaky}, boards("workable", 4), T0 + timedelta(hours=2))
    assert flaky.calls == 0


def test_a_fetch_with_no_jobs_is_a_success(store: SqliteStore) -> None:
    empty = FakeSource("lever", [])
    run(store, {"lever": empty}, boards("lever", 1), T0)
    health = store.source_health("lever")
    assert health is not None and health.last_ok_at == T0 and health.consecutive_failures == 0


def test_the_migration_adds_the_table_to_an_existing_database(tmp_path: Any) -> None:
    import sqlite3

    from career_copilot.adapters.store.migrations import MIGRATIONS, migrate

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    # A database at the version before this change: apply everything but the last, then migrate.
    for version, sql in MIGRATIONS[:-1]:
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    assert migrate(conn) == MIGRATIONS[-1][0]
    assert conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0] == 0
