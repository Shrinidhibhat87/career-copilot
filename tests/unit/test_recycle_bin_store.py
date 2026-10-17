"""The recycle bin in the store: the day count, the clean, and a cleaned job staying gone."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from career_copilot.adapters.store.migrations import MIGRATIONS, migrate
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import ScoreStage, UpsertOutcome
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from career_copilot.usecases.embed_jobs import job_text, text_hash
from tests.conftest import make_raw

NOW = datetime(2026, 9, 6, tzinfo=UTC)
DAYS = 7


def seed(store: SqliteStore, n: int = 1) -> int:
    """One passing posting with a match score and an embedding."""
    posting = to_posting(
        make_raw(source_job_id=str(n), url=f"https://x.test/{n}", title=f"Data Engineer {n}")
    )
    job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
    store.record_score(
        Score(
            job_id=job_id,
            stage=ScoreStage.MATCH,
            score=80,
            verdict=ScoreVerdict.from_score(80),
            rubric_version=RUBRIC_VERSION,
            model="m",
            prompt_hash="p",
            content_hash=posting.content_hash,
        ),
        NOW,
    )
    store.save_job_vectors("e", [(text_hash(job_text(posting)), [0.1, 0.2])], NOW)
    return job_id


def count(store: SqliteStore, sql: str, *args: object) -> int:
    return int(store._conn.execute(sql, args).fetchone()[0])


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore()


def test_a_job_dismissed_six_days_ago_has_one_day_left(store: SqliteStore) -> None:
    job = seed(store)
    store.record_decision(job, "dismissed", "", NOW)
    [item] = store.bin_items(NOW + timedelta(days=6), DAYS)
    assert (item.job_id, item.days_left) == (job, 1)
    assert item.title == "Data Engineer 1"
    assert item.dismissed_at == NOW


def test_at_seven_days_the_job_is_cleaned(store: SqliteStore) -> None:
    job = seed(store)
    store.record_decision(job, "dismissed", "", NOW)
    assert store.clean_expired(NOW + timedelta(days=7), DAYS) == 1
    assert store.bin_items(NOW + timedelta(days=7), DAYS) == []
    assert count(store, "SELECT COUNT(*) FROM jobs WHERE tombstoned_at IS NOT NULL") == 1


def test_bin_items_cleans_expired_first(store: SqliteStore) -> None:
    job = seed(store)
    store.record_decision(job, "dismissed", "", NOW)
    assert store.bin_items(NOW + timedelta(days=8), DAYS) == []
    assert count(store, "SELECT COUNT(*) FROM jobs WHERE tombstoned_at IS NOT NULL") == 1


def test_newest_dismissal_comes_first_and_saved_jobs_are_not_in_the_bin(
    store: SqliteStore,
) -> None:
    old, new, saved = seed(store, 1), seed(store, 2), seed(store, 3)
    store.record_decision(old, "dismissed", "", NOW)
    store.record_decision(new, "dismissed", "", NOW + timedelta(hours=1))
    store.record_decision(saved, "saved", "", NOW)
    assert [item.job_id for item in store.bin_items(NOW, DAYS)] == [new, old]
    assert store.bin_count(NOW, DAYS) == 2


def test_a_clean_drops_the_heavy_data_and_keeps_the_row(store: SqliteStore) -> None:
    job = seed(store)
    other = seed(store, 2)
    store.record_decision(job, "dismissed", "", NOW)
    store.clean_job(job, NOW)
    for table in ("scores", "job_versions"):
        assert count(store, f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", job) == 0
        assert count(store, f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", other) == 1
    assert count(store, "SELECT COUNT(*) FROM job_embeddings") == 1
    row = store._conn.execute(
        "SELECT description, tombstoned_at FROM jobs WHERE id = ?", (job,)
    ).fetchone()
    assert row["description"] == ""
    assert row["tombstoned_at"] == NOW.isoformat()


def test_upsert_of_a_cleaned_job_changes_nothing(store: SqliteStore) -> None:
    job = seed(store)
    store.record_decision(job, "dismissed", "", NOW)
    store.clean_job(job, NOW)
    store._conn.execute("UPDATE jobs SET needs_rescore = 0 WHERE id = ?", (job,))
    posting = to_posting(
        make_raw(
            source_job_id="1",
            url="https://x.test/1",
            title="Data Engineer 1",
            description="Rewritten advert, permanent full-time role with Python.",
        )
    )
    assert store.upsert_job(posting, apply_hard_filters(posting), NOW) == (
        job,
        UpsertOutcome.UNCHANGED,
    )
    row = store._conn.execute(
        "SELECT description, needs_rescore FROM jobs WHERE id = ?", (job,)
    ).fetchone()
    assert (row["description"], row["needs_rescore"]) == ("", 0)
    assert count(store, "SELECT COUNT(*) FROM job_versions WHERE job_id = ?", job) == 0


def test_restore_only_works_for_a_job_in_the_bin(store: SqliteStore) -> None:
    job, kept = seed(store, 1), seed(store, 2)
    store.record_decision(job, "dismissed", "", NOW)
    assert store.restore_job(kept, NOW) is False
    assert store.restore_job(job, NOW) is True
    assert store.bin_items(NOW, DAYS) == []
    assert store.restore_job(job, NOW) is False
    store.record_decision(job, "dismissed", "", NOW)
    store.clean_job(job, NOW)
    assert store.restore_job(job, NOW) is False


def test_migration_keeps_existing_jobs_untombstoned(tmp_path: Path) -> None:
    version = next(v for v, sql in MIGRATIONS if "tombstoned_at" in sql)
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for v, sql in MIGRATIONS:
        if v < version:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (v,))
    conn.execute(
        "INSERT INTO jobs (source, source_job_id, company, company_norm, title, title_norm,"
        " location, location_norm, canonical_url, description, fingerprint, content_hash,"
        " filter_outcome, filter_reasons, filter_warnings, status, needs_rescore,"
        " first_seen_at, last_seen_at) VALUES ('g','1','A','a','T','t','L','l','u','d','f',"
        " 'h','pass','','','active',0,'x','x')"
    )
    conn.commit()
    migrate(conn)
    assert conn.execute("SELECT description, tombstoned_at FROM jobs").fetchall() == [("d", None)]
