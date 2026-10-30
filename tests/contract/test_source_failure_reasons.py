"""Why a source failed, and how long it took, from the fetch to the run view and digest."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_copilot.adapters.notify.markdown import MarkdownNotifier
from career_copilot.adapters.sources.failures import read_failure
from career_copilot.adapters.store.migrations import MIGRATIONS, migrate
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.models import SourceTarget
from career_copilot.usecases.digest import DigestRun
from career_copilot.usecases.discover import DiscoverRun
from tests.conftest import FakeSource, make_raw

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as s:
        yield s


class Timeout:
    name = "lever"

    def fetch(self, target: SourceTarget) -> list[Any]:
        raise httpx.ReadTimeout("read timed out for https://api.lever.co/v0/postings/acme?secret=1")


def test_a_failed_fetch_stores_its_reason_and_how_long_it_took(store: SqliteStore) -> None:
    DiscoverRun(
        {"lever": Timeout()},
        store,
        [SourceTarget("lever", "acme")],
        lambda: NOW,
        read_failure=read_failure,
    ).execute()
    row = store._conn.execute("SELECT ok, reason, duration_ms FROM source_runs").fetchone()
    assert (row["ok"], row["reason"]) == (0, "timeout")
    assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0


def test_a_success_stores_how_long_it_took_and_no_reason(store: SqliteStore) -> None:
    source = FakeSource("greenhouse", [make_raw()])
    DiscoverRun(
        {"greenhouse": source}, store, [SourceTarget("greenhouse", "acme")], lambda: NOW
    ).execute()
    row = store._conn.execute("SELECT ok, reason, duration_ms FROM source_runs").fetchone()
    assert row["ok"] == 1 and row["reason"] is None and row["duration_ms"] is not None


def test_the_run_view_says_why_and_never_shows_the_raw_error(store: SqliteStore) -> None:
    run_id = store.start_run(NOW)
    store.record_source_run(
        run_id, "lever", "acme", False, 0, "boom https://x.test/?secret=1", NOW, reason="timeout"
    )
    store.finish_run(run_id, NOW, "failed=1")
    run = store.get_run(run_id)
    assert run is not None
    assert run["source_failures"] == [
        {"source": "lever", "identifier": "acme", "message": "took too long to answer"}
    ]


def test_a_row_without_a_reason_keeps_the_old_message(store: SqliteStore) -> None:
    run_id = store.start_run(NOW)
    store.record_source_run(run_id, "lever", "old", False, 0, "boom", NOW)
    run = store.get_run(run_id)
    assert run is not None
    assert run["source_failures"][0]["message"] == "Source could not be fetched. Try again later."


def test_the_digest_uses_the_reason_and_falls_back_to_the_error(
    store: SqliteStore, tmp_path: Path
) -> None:
    run_id = store.start_run(NOW)
    store.record_source_run(run_id, "lever", "a", False, 0, "boom", NOW, reason="forbidden")
    store.record_source_run(run_id, "lever", "b", False, 0, "old error", NOW)
    store.finish_run(run_id, NOW, "failed=2")
    body = DigestRun(store, MarkdownNotifier(tmp_path / "d.md"), clock=lambda: NOW).execute()
    assert "lever:a — refused access" in body
    assert "lever:b — old error" in body


def test_the_migration_adds_both_columns_to_an_existing_database(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for version, sql in MIGRATIONS:
        if version < 33:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    assert migrate(conn) == 33
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_runs)")}
    assert {"reason", "duration_ms"} <= columns
