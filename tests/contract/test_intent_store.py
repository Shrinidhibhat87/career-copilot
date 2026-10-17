"""Store contract for the intent log: append-only entries, archived not deleted."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from career_copilot.adapters.store import migrations
from career_copilot.adapters.store.migrations import migrate
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.intent import IntentKind

T = datetime(2026, 10, 7, tzinfo=UTC)


def test_entries_are_appended_and_listed_oldest_first() -> None:
    store = SqliteStore()
    store.add_intent("FDE roles", T)
    store.add_intent("Berlin only", T + timedelta(minutes=1))
    assert [e.text for e in store.active_intent()] == ["FDE roles", "Berlin only"]


def test_archived_entries_are_kept_but_not_active() -> None:
    store = SqliteStore()
    entry = store.add_intent("old idea", T)
    assert store.archive_intent(entry.id, T) is True
    assert store.active_intent() == []
    assert store.archive_intent(entry.id, T) is False  # already archived
    row = store._conn.execute("SELECT archived_at FROM intent_entries").fetchone()
    assert row["archived_at"] == T.isoformat()


def test_archiving_an_unknown_id_is_false() -> None:
    assert SqliteStore().archive_intent(999, T) is False


def test_condensed_kind_round_trips() -> None:
    store = SqliteStore()
    store.add_intent("note", T, kind=IntentKind.CONDENSED)
    assert store.active_intent()[0].kind is IntentKind.CONDENSED


def test_entries_survive_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    with SqliteStore(path) as store:
        added = store.add_intent("FDE roles", T)
    with SqliteStore(path) as store:
        assert store.active_intent() == [added]


def test_migration_seeds_latest_role_intent_once(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    # Bring a database to v11 with two saved preference versions.
    for version, sql in migrations.MIGRATIONS:
        if version <= 11:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    for intent in ("old wish", "  FDE in Berlin  "):
        conn.execute(
            "INSERT INTO preferences (payload, prompt_hash, created_at) VALUES (?,?,?)",
            (json.dumps({"role_intent": intent}), "h", T.isoformat()),
        )
    conn.commit()
    migrate(conn)
    migrate(conn)  # idempotent: v12 is applied once
    rows = conn.execute("SELECT text, kind FROM intent_entries").fetchall()
    assert [(r["text"], r["kind"]) for r in rows] == [("FDE in Berlin", "entry")]


def test_migration_seeds_nothing_for_blank_role_intent() -> None:
    store = SqliteStore()  # fresh database: no preferences rows at all
    assert store.active_intent() == []
