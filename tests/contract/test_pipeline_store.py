"""Migration 38 and the application stage tables (spec 021)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from career_copilot.adapters.store import migrations
from career_copilot.adapters.store.migrations import migrate
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from tests.conftest import make_raw

NOW = datetime(2026, 11, 11, 9, tzinfo=UTC)


def seed(store: SqliteStore, n: int = 1) -> int:
    posting = to_posting(
        make_raw(source_job_id=str(n), url=f"https://x.test/{n}", title=f"Data Engineer {n}")
    )
    job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
    return job_id


def test_migration_38_backfills_an_applied_stage_for_applied_jobs(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for version, sql in migrations.MIGRATIONS:
        if version <= 37:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    for job, decision in ((1, "applied"), (2, "applied"), (2, "saved"), (3, "saved")):
        conn.execute(
            "INSERT INTO job_decisions (job_id, decision, created_at) VALUES (?,?,?)",
            (job, decision, NOW.isoformat()),
        )
    conn.commit()
    assert migrate(conn) == 38
    stages = conn.execute("SELECT job_id, stage FROM application_stages").fetchall()
    assert [tuple(r) for r in stages] == [(1, "applied")]


def test_applying_starts_the_pipeline_and_applying_again_resumes_it(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        job = seed(store)
        assert store.application_stage(job) is None
        store.record_decision(job, "applied", "", NOW)
        assert store.application_stage(job) == ("applied", NOW)
        store.record_stage(job, "interviewing", "phone screen", NOW + timedelta(days=3))
        store.undo_applied(job, NOW + timedelta(days=4))
        assert store.application_cards(NOW, 14) == []
        store.record_decision(job, "applied", "", NOW + timedelta(days=5))
        [card] = store.application_cards(NOW + timedelta(days=5), 14)
        assert card["stage"] == "interviewing"
        assert [t["stage"] for t in store.stage_timeline(job)] == ["applied", "interviewing"]


def test_a_stage_needs_an_applied_job(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        job = seed(store)
        with pytest.raises(LookupError):
            store.record_stage(job, "offer", None, NOW)


def test_cards_carry_note_hidden_and_staleness(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        job = seed(store)
        store.record_decision(job, "applied", "", NOW)
        store.set_card_note(job, "Call Anna on Friday", NOW)
        store.set_card_hidden(job, True, NOW)
        later = NOW + timedelta(days=16)
        [card] = store.application_cards(later, 14)
        assert (card["note"], card["hidden"], card["daysQuiet"], card["stale"]) == (
            "Call Anna on Friday",
            True,
            16,
            True,
        )
        store.set_card_hidden(job, False, later)
        assert store.application_cards(later, 14)[0]["hidden"] is False


def test_stale_days_default_and_round_trip(tmp_path: Path) -> None:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        assert store.stale_days() == 14
        store.set_stale_days(30)
        assert store.stale_days() == 30
