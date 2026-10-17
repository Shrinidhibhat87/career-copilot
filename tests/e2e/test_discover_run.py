"""End-to-end discovery on fakes. The rerun-idempotency test is the important one."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from career_copilot.adapters.sources.ats import GreenhouseSource, LeverSource
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.models import FilterOutcome, SourceTarget
from career_copilot.ports import JobSource
from career_copilot.usecases.discover import DiscoverRun
from tests.conftest import FakeSource, make_raw

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as s:
        yield s


def clock_at(moment: datetime) -> Any:
    return lambda: moment


def berlin_jobs(count: int) -> list[Any]:
    return [
        make_raw(
            source_job_id=str(i),
            title=f"Forward Deployed Engineer {i}",
            url=f"https://boards.greenhouse.io/acme/jobs/{i}",
        )
        for i in range(count)
    ]


def test_run_stores_and_classifies(store: SqliteStore, now: datetime) -> None:
    source = FakeSource(
        "greenhouse",
        [*berlin_jobs(3), make_raw(source_job_id="9", title="Head of AI", url="https://x.com/9")],
    )
    report = DiscoverRun(
        {"greenhouse": source}, store, [SourceTarget("greenhouse", "acme")], clock_at(now)
    ).execute()

    assert report.fetched == 4
    assert report.inserted == 4
    assert report.qualified == 3
    assert report.rejected == 1
    assert len(store.list_jobs(FilterOutcome.PASS, limit=50)) == 3


def test_rerun_creates_no_duplicates_and_no_rescores(store: SqliteStore, now: datetime) -> None:
    """The single most important assertion in the suite."""
    source = FakeSource("greenhouse", berlin_jobs(3))
    targets = [SourceTarget("greenhouse", "acme", cooldown_hours=24)]

    first = DiscoverRun({"greenhouse": source}, store, targets, clock_at(now)).execute()
    later = now + timedelta(hours=25)  # past the cooldown, so it really refetches
    second = DiscoverRun({"greenhouse": source}, store, targets, clock_at(later)).execute()

    assert first.inserted == 3
    assert second.inserted == 0
    assert second.changed == 0
    assert second.unchanged == 3
    assert len(store.list_jobs(limit=50)) == 3
    assert source.calls == 2


def test_cooldown_prevents_immediate_refetch(store: SqliteStore, now: datetime) -> None:
    source = FakeSource("greenhouse", berlin_jobs(2))
    targets = [SourceTarget("greenhouse", "acme", cooldown_hours=24)]

    DiscoverRun({"greenhouse": source}, store, targets, clock_at(now)).execute()
    second = DiscoverRun(
        {"greenhouse": source}, store, targets, clock_at(now + timedelta(hours=2))
    ).execute()

    assert source.calls == 1
    assert second.fetched == 0
    assert any("cooldown" in s for s in second.skipped_targets)


def test_broken_source_does_not_stop_the_run(store: SqliteStore, now: datetime) -> None:
    sources: dict[str, JobSource] = {
        "greenhouse": FakeSource("greenhouse", berlin_jobs(2)),
        "lever": FakeSource("lever", [], fail=True),
    }
    report = DiscoverRun(
        sources,
        store,
        [SourceTarget("lever", "dead", tier=0), SourceTarget("greenhouse", "acme", tier=0)],
        clock_at(now),
    ).execute()

    assert report.inserted == 2
    assert report.failed_sources == ("lever:dead",)


def test_failed_fetch_is_recorded_so_it_is_not_read_as_zero_jobs(
    store: SqliteStore, now: datetime
) -> None:
    DiscoverRun(
        {"lever": FakeSource("lever", [], fail=True)},
        store,
        [SourceTarget("lever", "dead")],
        clock_at(now),
    ).execute()
    row = store._conn.execute("SELECT ok, fetched, error FROM source_runs").fetchone()
    assert (row["ok"], row["fetched"]) == (0, 0)
    assert "unreachable" in row["error"]


def test_expensive_tiers_are_skipped_once_the_target_is_met(
    store: SqliteStore, now: datetime
) -> None:
    cheap = FakeSource("greenhouse", berlin_jobs(6))
    expensive = FakeSource("lever", berlin_jobs(4))
    report = DiscoverRun(
        {"greenhouse": cheap, "lever": expensive},
        store,
        [SourceTarget("lever", "paid", tier=2), SourceTarget("greenhouse", "acme", tier=0)],
        clock_at(now),
        stop_after=6,
    ).execute()

    assert cheap.calls == 1
    assert expensive.calls == 0
    assert "lever:paid (target met)" in report.skipped_targets


def test_expensive_tier_runs_when_cheap_tier_is_thin(store: SqliteStore, now: datetime) -> None:
    cheap = FakeSource("greenhouse", berlin_jobs(1))
    expensive = FakeSource(
        "lever", [make_raw(source_job_id="l1", url="https://x.com/l1", title="AI Engineer")]
    )
    DiscoverRun(
        {"greenhouse": cheap, "lever": expensive},
        store,
        [SourceTarget("lever", "paid", tier=2), SourceTarget("greenhouse", "acme", tier=0)],
        clock_at(now),
        stop_after=6,
    ).execute()

    assert cheap.calls == 1
    assert expensive.calls == 1


def test_changed_posting_is_flagged_on_the_next_run(store: SqliteStore, now: datetime) -> None:
    source = FakeSource("greenhouse", berlin_jobs(1))
    targets = [SourceTarget("greenhouse", "acme", cooldown_hours=1)]
    DiscoverRun({"greenhouse": source}, store, targets, clock_at(now)).execute()

    source.jobs = [
        make_raw(
            source_job_id="0",
            title="Forward Deployed Engineer 0",
            url="https://boards.greenhouse.io/acme/jobs/0",
            description="Rewritten. Now requires fluent German.",
        )
    ]
    second = DiscoverRun(
        {"greenhouse": source}, store, targets, clock_at(now + timedelta(hours=2))
    ).execute()

    assert second.changed == 1
    assert second.inserted == 0
    assert store.list_jobs(FilterOutcome.REJECT, limit=10)[0].posting.source_job_id == "0"


def test_real_adapters_run_end_to_end_on_recorded_payloads(
    store: SqliteStore, now: datetime
) -> None:
    """Wires the actual parsers into the pipeline, still with zero network."""
    gh = GreenhouseSource(lambda url: json.loads((FIXTURES / "greenhouse_board.json").read_text()))
    lever = LeverSource(lambda url: json.loads((FIXTURES / "lever_board.json").read_text()))
    report = DiscoverRun(
        {"greenhouse": gh, "lever": lever},
        store,
        [SourceTarget("greenhouse", "acme"), SourceTarget("lever", "beta")],
        clock_at(now),
    ).execute()

    assert report.fetched == 4
    assert report.inserted == 4
    # Head of Platform and the working-student role are rejected.
    assert report.rejected == 3
    assert report.qualified == 1


def test_a_large_target_keeps_collecting_past_the_first_source(
    store: SqliteStore, now: datetime
) -> None:
    """The initial sweep must not halt after the first company."""
    first = FakeSource("greenhouse", berlin_jobs(6))
    second = FakeSource(
        "lever",
        [
            make_raw(
                source="lever",
                source_job_id=f"l{i}",
                title=f"AI Engineer {i}",
                url=f"https://jobs.lever.co/beta/{i}",
            )
            for i in range(4)
        ],
    )
    report = DiscoverRun(
        {"greenhouse": first, "lever": second},
        store,
        [SourceTarget("greenhouse", "acme", tier=0), SourceTarget("lever", "beta", tier=0)],
        clock_at(now),
        stop_after=50,
    ).execute()

    assert (first.calls, second.calls) == (1, 1)
    assert report.inserted == 10
    assert report.skipped_targets == ()


def test_the_target_stops_a_free_board_too(store: SqliteStore, now: datetime) -> None:
    """Reverses `test_tier_zero_targets_are_all_visited_after_target_is_met`.

    Draining every free board looked harmless while free meant cheap. It is not free
    downstream: every row it collects is a row the user reads past, and with all eight
    configured targets at tier 0 the guard could never fire at all — 1071 rows, 990 of
    them rejects. The ladder still decides the order boards are visited; the day's
    target now decides when to stop, whatever tier the next board is.
    """
    first = FakeSource("greenhouse", berlin_jobs(6))
    second = FakeSource("lever", berlin_jobs(4))
    report = DiscoverRun(
        {"greenhouse": first, "lever": second},
        store,
        [SourceTarget("greenhouse", "acme", tier=0), SourceTarget("lever", "beta", tier=0)],
        clock_at(now),
        stop_after=6,
    ).execute()

    assert (first.calls, second.calls) == (1, 0)
    assert report.skipped_targets == ("lever:beta (target met)",)


def test_one_board_cannot_be_the_whole_run(store: SqliteStore, now: datetime) -> None:
    """A single Greenhouse board supplied 361 of the first 1071 stored jobs."""
    board = FakeSource("greenhouse", berlin_jobs(30))
    report = DiscoverRun(
        {"greenhouse": board},
        store,
        [SourceTarget("greenhouse", "acme", tier=0)],
        clock_at(now),
        stop_after=50,
        max_jobs_per_target=10,
    ).execute()

    assert report.fetched == 10
    assert report.inserted == 10
    # The ledger records what was examined, never what the board offered.
    assert report.source_runs[0].fetched == 10


def test_new_jobs_go_through_the_repost_check(store: SqliteStore, now: datetime) -> None:
    seen: list[list[int]] = []

    def check(ids: Any, at: datetime) -> int:
        seen.append(list(ids))
        return 0

    source = FakeSource("greenhouse", berlin_jobs(2))
    target = [SourceTarget("greenhouse", "acme")]
    run = DiscoverRun({"greenhouse": source}, store, target, clock_at(now), hide_reposts=check)
    run.execute()
    # A second run finds nothing new, so there is nothing to check.
    DiscoverRun(
        {"greenhouse": source}, store, target, clock_at(now + timedelta(days=1)), hide_reposts=check
    ).execute()

    assert len(seen) == 1
    assert len(seen[0]) == 2


def test_a_visited_board_goes_behind_boards_never_visited(
    store: SqliteStore, now: datetime
) -> None:
    """With `stop_after` met by the first board, the rest used to wait behind it forever."""
    # Distinct jobs per board: identical ones would dedupe and never meet `stop_after`.
    sources: dict[str, JobSource] = {
        name: FakeSource(
            name,
            [
                make_raw(
                    source_job_id=f"{name}-{i}",
                    title=f"Forward Deployed Engineer {name} {i}",
                    url=f"https://example.com/{name}/{i}",
                )
                for i in range(3)
            ],
        )
        for name in ("greenhouse", "lever", "ashby")
    }
    targets = [
        SourceTarget("greenhouse", "a", cooldown_hours=24),
        SourceTarget("lever", "b", cooldown_hours=24),
        SourceTarget("ashby", "c", cooldown_hours=24),
    ]

    def run(moment: datetime) -> Any:
        return DiscoverRun(sources, store, targets, clock_at(moment), stop_after=3).execute()

    first = run(now)
    second = run(now + timedelta(hours=25))
    third = run(now + timedelta(hours=50))

    assert [r.source for r in first.source_runs] == ["greenhouse"]
    assert [r.source for r in second.source_runs] == ["lever"]
    assert [r.source for r in third.source_runs] == ["ashby"]


def test_the_longest_waiting_board_is_visited_first(store: SqliteStore, now: datetime) -> None:
    store.set_cooldown("greenhouse", "a", now - timedelta(days=1), hours=1)
    store.set_cooldown("lever", "b", now - timedelta(days=5), hours=1)
    sources: dict[str, JobSource] = {
        name: FakeSource(name, berlin_jobs(1)) for name in ("greenhouse", "lever", "ashby")
    }
    targets = [
        SourceTarget("greenhouse", "a"),
        SourceTarget("lever", "b"),
        SourceTarget("ashby", "c"),
    ]

    report = DiscoverRun(sources, store, targets, clock_at(now)).execute()

    # Never visited first, then the older visit, then the newer one.
    assert [r.source for r in report.source_runs] == ["ashby", "lever", "greenhouse"]


def test_tier_still_comes_before_how_long_a_board_has_waited(
    store: SqliteStore, now: datetime
) -> None:
    store.set_cooldown("greenhouse", "a", now - timedelta(days=9), hours=1)
    sources: dict[str, JobSource] = {
        name: FakeSource(name, berlin_jobs(1)) for name in ("greenhouse", "lever")
    }
    targets = [SourceTarget("greenhouse", "a", tier=0), SourceTarget("lever", "b", tier=1)]

    report = DiscoverRun(sources, store, targets, clock_at(now)).execute()

    assert [r.source for r in report.source_runs] == ["greenhouse", "lever"]


def test_boards_never_visited_keep_their_configured_order(
    store: SqliteStore, now: datetime
) -> None:
    sources: dict[str, JobSource] = {
        name: FakeSource(name, berlin_jobs(1)) for name in ("greenhouse", "lever", "ashby")
    }
    targets = [
        SourceTarget("lever", "b"),
        SourceTarget("ashby", "c"),
        SourceTarget("greenhouse", "a"),
    ]

    report = DiscoverRun(sources, store, targets, clock_at(now)).execute()

    assert [r.source for r in report.source_runs] == ["lever", "ashby", "greenhouse"]


def test_a_failed_board_rotates_to_the_back_too(store: SqliteStore, now: datetime) -> None:
    sources: dict[str, JobSource] = {
        "greenhouse": FakeSource("greenhouse", [], fail=True),
        "lever": FakeSource("lever", berlin_jobs(1)),
    }
    targets = [SourceTarget("greenhouse", "a"), SourceTarget("lever", "b")]
    DiscoverRun(sources, store, targets, clock_at(now)).execute()

    visited = store.last_scraped_by_target()
    assert set(visited) == {("greenhouse", "a"), ("lever", "b")}
