"""Migration 37 and the notification tables (spec 020)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from career_copilot.adapters.store import migrations
from career_copilot.adapters.store.migrations import migrate
from career_copilot.adapters.store.sqlite_store import SqliteStore

NOW = datetime(2026, 11, 4, 9, tzinfo=UTC)


def test_migration_37_adds_the_tables_to_an_existing_database(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for version, sql in migrations.MIGRATIONS:
        if version <= 36:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    conn.commit()
    assert migrate(conn) == 37
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"notification_settings", "notified_jobs", "telegram_state"} <= tables


def test_nothing_saved_reads_as_none(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        assert store.notification_setting() is None


def test_versions_round_trip_and_the_newest_wins(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        store.save_notification_setting(True, True, False, NOW)
        store.save_notification_setting(True, False, False, NOW + timedelta(hours=1))
        saved = store.notification_setting()
    assert saved is not None and saved.version == 2 and saved.telegram is False


def test_a_job_is_marked_once_per_channel(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        store.mark_notified(4, "telegram", NOW)
        store.mark_notified(4, "telegram", NOW)
        store.mark_notified(4, "email", NOW)
        assert store.notified_job_ids("telegram") == {4}
        assert store.notified_job_ids("email") == {4}


def test_the_offset_round_trips(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        assert store.telegram_offset() == 0
        store.set_telegram_offset(12)
        store.set_telegram_offset(15)
        assert store.telegram_offset() == 15
