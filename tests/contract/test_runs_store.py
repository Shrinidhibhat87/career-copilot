"""Run-ledger guarantees that must hold at the SQLite boundary."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.runs import HEARTBEAT_TIMEOUT, Progress, RunConfig, RunInProgress

NOW = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def frozen(**overrides: object) -> RunConfig:
    values: dict[str, object] = {
        "preferences": Preferences(core_skills=("python",), version=3),
        "triage_model": "triage-v1",
        "match_model": "match-v1",
        "rubric_version": 2,
        "mode": "incremental",
        "budget_eur": 1.0,
        "target": 6,
        "triage_reserve_eur": 0.02,
        "match_reserve_eur": 0.2,
        "triage_max_output_tokens": 256,
        "match_max_output_tokens": 1024,
    }
    values.update(overrides)
    return RunConfig(**values)  # type: ignore[arg-type]


def test_run_configuration_round_trips_as_an_immutable_snapshot(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        expected = frozen()
        run_id = store.begin_run("full", expected, NOW, os.getpid(), "birth")
        assert store.run_config(run_id) == expected


def test_two_connections_compete_atomically_for_one_slot(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    SqliteStore(path).close()
    barrier = Barrier(2)

    def begin() -> str:
        with SqliteStore(path) as store:
            barrier.wait()
            try:
                store.begin_run("full", frozen(), NOW, os.getpid(), "birth")
            except RunInProgress:
                return "busy"
            return "running"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(begin) for _ in range(2)]
        outcomes = sorted(future.result() for future in futures)
    assert outcomes == ["busy", "running"]
    with SqliteStore(path) as store:
        count = store._conn.execute("SELECT COUNT(*) FROM runs WHERE state='running'").fetchone()[0]
        assert count == 1


def test_reads_and_fresh_heartbeats_never_reclaim_a_live_run(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        run_id = store.begin_run("full", frozen(), NOW, os.getpid(), "birth")
        changes = store._conn.total_changes
        for _ in range(100):
            assert store.get_run(run_id)["state"] == "running"  # type: ignore[index]
        assert store._conn.total_changes == changes
        assert store.reclaim_dead_run(NOW + timedelta(seconds=HEARTBEAT_TIMEOUT - 1)) is None
        assert store.get_run(run_id)["state"] == "running"  # type: ignore[index]


def test_only_a_stale_run_with_a_dead_owner_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        run_id = store.begin_run("full", frozen(), NOW, 999_999, "old-birth")
        stale = NOW + timedelta(seconds=HEARTBEAT_TIMEOUT + 1)

        def missing(pid: int, signal: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr("career_copilot.adapters.store.sqlite_store.os.kill", missing)
        assert store.reclaim_dead_run(stale) == run_id
        row = store.get_run(run_id)
        assert row is not None
        assert (row["state"], row["failure_category"]) == ("failed", "interrupted")


def test_a_stale_run_with_the_same_live_process_identity_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        run_id = store.begin_run("full", frozen(), NOW, 4242, "same-birth")
        monkeypatch.setattr(
            "career_copilot.adapters.store.sqlite_store.os.kill", lambda pid, signal: None
        )
        monkeypatch.setattr(
            "career_copilot.adapters.store.sqlite_store.process_started_at",
            lambda pid: "same-birth",
        )
        stale = NOW + timedelta(seconds=HEARTBEAT_TIMEOUT + 1)
        assert store.reclaim_dead_run(stale) is None
        row = store.get_run(run_id)
        assert row is not None
        assert row["state"] == "running"


def test_progress_and_terminal_updates_are_conditional(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        run_id = store.begin_run("full", frozen(), NOW, os.getpid(), "birth")
        store.update_run_progress(run_id, Progress("score", 2, 5, 0.12, 1.0, 1), NOW)
        store.abort_run(run_id, NOW, "ceiling reached")
        store.fail_run(run_id, NOW, "internal", "late failure")
        row = store.get_run(run_id)
        assert row is not None
        assert (row["stage"], row["completed"], row["failed"]) == ("score", 2, 1)
        assert (row["state"], row["failure_category"]) == ("aborted", "budget")


def test_a_company_sync_is_syncing_and_never_competes_with_a_scoring_run(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        sync_id = store.start_run(NOW, kind="company_sync")
        row = store.get_run(sync_id)
        assert row is not None and row["state"] == "syncing"

        # The single-active-run index covers `running` only: a scoring run starts fine
        # mid-sync, and a second sync can be started beside the first.
        store.begin_run("full", frozen(), NOW, os.getpid(), "birth")
        assert store.start_run(NOW, kind="company_sync") != sync_id


def test_a_syncing_run_takes_progress_and_ends_succeeded_or_failed(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        done = store.start_run(NOW, kind="company_sync")
        store.update_run_progress(done, Progress("resolving", 3, 9, 0.0, 0.0), NOW)
        progressed = store.get_run(done)
        assert progressed is not None
        assert (progressed["state"], progressed["completed"], progressed["total"]) == (
            "syncing",
            3,
            9,
        )
        store.finish_run(done, NOW, "fetched 9")
        finished = store.get_run(done)
        assert finished is not None and finished["state"] == "succeeded"

        broken = store.start_run(NOW, kind="company_sync")
        store.fail_run(broken, NOW, "error", "Notion unreachable")
        failed = store.get_run(broken)
        assert failed is not None
        assert (failed["state"], failed["failure_message"]) == ("failed", "Notion unreachable")


def test_only_a_stale_syncing_run_is_reclaimed(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        fresh = store.start_run(NOW, kind="company_sync")
        stale = store.start_run(
            NOW - timedelta(seconds=HEARTBEAT_TIMEOUT + 60), kind="company_sync"
        )
        assert store.reclaim_stale_syncs(NOW) == [stale]
        fresh_row, stale_row = store.get_run(fresh), store.get_run(stale)
        assert fresh_row is not None and fresh_row["state"] == "syncing"
        assert stale_row is not None
        assert (stale_row["state"], stale_row["failure_category"]) == ("failed", "interrupted")


def test_a_dead_syncing_run_does_not_count_as_the_active_sync(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        live = store.start_run(NOW, kind="company_sync")
        store.record_company_sync(live, "notion")
        assert store.active_company_sync("notion", NOW) == live
        assert store.active_company_sync("csv", NOW) is None  # a different list
        later = NOW + timedelta(seconds=HEARTBEAT_TIMEOUT + 60)
        assert store.active_company_sync("notion", later) is None
