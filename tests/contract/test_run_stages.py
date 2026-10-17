from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from career_copilot.adapters.store import migrations
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.runs import RunConfig

T0 = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def begin(store: SqliteStore) -> int:
    config = RunConfig(
        Preferences(core_skills=("python",)), "t", "m", 1, "initial", 1.0, 1, 0.02, 0.2, 256, 1024
    )
    return store.begin_run("full", config, T0, os.getpid(), "birth")


def test_a_finished_stage_reports_its_seconds(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        run_id = begin(store)
        store.start_stage(run_id, "discover", T0)
        store.end_stage(run_id, "discover", T0 + timedelta(seconds=12.34))
        row = store.get_run(run_id)
    assert row is not None
    [stage] = row["stages"]
    assert stage["stage"] == "discover"
    assert stage["seconds"] == 12.3


def test_an_unfinished_stage_reads_as_open(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        run_id = begin(store)
        store.start_stage(run_id, "score", T0)
        row = store.get_run(run_id)
    assert row is not None
    assert row["stages"][0]["endedAt"] is None
    assert row["stages"][0]["seconds"] is None


def test_the_funnel_round_trips_and_is_none_when_never_recorded(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        run_id = begin(store)
        before = store.get_run(run_id)
        store.record_funnel(run_id, {"score": {"above": 18}})
        after = store.get_run(run_id)
    assert before is not None and before["funnel"] is None
    assert after is not None and after["funnel"] == {"score": {"above": 18}}


def test_migration_23_keeps_older_runs(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for version, sql in migrations.MIGRATIONS:
        if version <= 22:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    conn.execute(
        "INSERT INTO runs (started_at, kind, state) VALUES ('2026-01-01', 'full', 'succeeded')"
    )
    conn.commit()
    migrations.migrate(conn)
    row = conn.execute("SELECT funnel FROM runs").fetchone()
    assert row["funnel"] is None
    assert conn.execute("SELECT COUNT(*) FROM run_stages").fetchone()[0] == 0
