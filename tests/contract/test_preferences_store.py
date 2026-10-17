"""Preferences persistence, and what `preferences_hash` does to score identity."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_copilot.adapters.store.migrations import migrate
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import ScoreStage
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.preferences import LanguageSkill, Preferences
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from tests.conftest import make_raw

MODEL = "m"


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as opened:
        yield opened


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def make_prefs(**overrides: object) -> Preferences:
    defaults: dict[str, object] = {
        "core_skills": ("python", "agentic"),
        "allowed_cities": ("berlin", "remote", ""),
        "languages": (LanguageSkill("German", "B1"),),
        "notes": "prose",
    }
    defaults.update(overrides)
    return Preferences(**defaults)  # type: ignore[arg-type]


def add_job(store: SqliteStore, now: datetime, **overrides: object) -> int:
    posting = to_posting(make_raw(**overrides))
    job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), now)
    return job_id


def make_score(store: SqliteStore, job_id: int, prefs_hash: str, version: int = 1) -> Score:
    row = store._conn.execute("SELECT content_hash FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Score(
        job_id=job_id,
        stage=ScoreStage.MATCH,
        score=80,
        verdict=ScoreVerdict.STRONG,
        rubric_version=RUBRIC_VERSION,
        model=MODEL,
        prompt_hash="ph",
        content_hash=str(row["content_hash"]),
        evidence_refs=("Built a thing",),
        preferences_version=version,
        preferences_hash=prefs_hash,
    )


# --- round trip -----------------------------------------------------------


def test_a_fresh_database_holds_no_preferences(store: SqliteStore) -> None:
    """None, not a default: it is what makes the caller fall back to the bootstrap."""
    assert store.load_current() is None


def test_saving_assigns_version_one_then_two(store: SqliteStore, now: datetime) -> None:
    first = store.save(make_prefs(), now)
    second = store.save(make_prefs(notes="changed"), now)
    assert (first.version, second.version) == (1, 2)
    assert store.load_current() is not None
    current = store.load_current()
    assert current is not None
    assert current.version == 2


def test_a_saved_version_round_trips_every_field(store: SqliteStore, now: datetime) -> None:
    saved = store.save(make_prefs(seniority=("mid", "staff"), batch_size=11), now)
    loaded = store.load_version(saved.version)
    assert loaded is not None
    assert loaded.to_payload() == saved.to_payload()
    assert loaded.prompt_hash() == saved.prompt_hash()
    assert loaded.updated_at == now


def test_an_earlier_version_is_still_readable(store: SqliteStore, now: datetime) -> None:
    first = store.save(make_prefs(), now)
    store.save(make_prefs(notes="later"), now)
    earlier = store.load_version(first.version)
    assert earlier is not None
    assert earlier.notes == "prose"


def test_an_unknown_version_is_none(store: SqliteStore) -> None:
    assert store.load_version(99) is None


# --- migration ------------------------------------------------------------


def _v2_database(path: Path) -> sqlite3.Connection:
    """A database stopped at migration 2, as an existing install would be."""
    from career_copilot.adapters.store import migrations

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for version, sql in migrations.MIGRATIONS:
        if version > 2:
            break
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    conn.commit()
    return conn


def test_migration_three_upgrades_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    conn = _v2_database(path)
    conn.execute(
        """
        INSERT INTO jobs (source, source_job_id, company, company_norm, title, title_norm,
            location, location_norm, canonical_url, description, fingerprint, content_hash,
            filter_outcome, first_seen_at, last_seen_at)
        VALUES ('gh','1','A','a','T','t','Berlin','berlin','u','d','f','ch','pass','x','x')
        """
    )
    conn.execute(
        """
        INSERT INTO scores (job_id, stage, score, verdict, rubric_version, model,
            prompt_hash, content_hash, created_at)
        VALUES (1,'match',80,'strong',1,'m','ph','ch','x')
        """
    )
    conn.commit()
    assert migrate(conn) == 31

    row = conn.execute("SELECT * FROM scores").fetchone()
    # A row written before preferences existed is backfilled to the empty hash, which
    # never equals a real one — so it reads as stale rather than as current.
    assert row["preferences_hash"] == ""
    assert row["preferences_version"] == 0
    assert row["cost_eur"] is None
    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_scores_identity'"
    ).fetchone()
    assert "preferences_hash" in index["sql"]
    conn.close()


def test_the_identity_index_admits_the_same_score_under_new_preferences(
    store: SqliteStore, now: datetime
) -> None:
    job_id = add_job(store, now)
    assert store.record_score(make_score(store, job_id, "hash-a"), now) != 0
    assert store.record_score(make_score(store, job_id, "hash-a"), now) == 0
    assert store.record_score(make_score(store, job_id, "hash-b", version=2), now) != 0


# --- the three identity-aware queries -------------------------------------


def test_a_score_under_other_preferences_leaves_the_job_open(
    store: SqliteStore, now: datetime
) -> None:
    job_id = add_job(store, now)
    store.record_score(make_score(store, job_id, "hash-a"), now)
    assert store.jobs_needing_score(MODEL, RUBRIC_VERSION, preferences_hash="hash-a") == []
    still_open = store.jobs_needing_score(MODEL, RUBRIC_VERSION, preferences_hash="hash-b")
    assert [job.id for job in still_open] == [job_id]


def test_the_digest_only_shows_scores_from_the_preferences_in_force(
    store: SqliteStore, now: datetime
) -> None:
    job_id = add_job(store, now)
    store.record_score(make_score(store, job_id, "hash-a"), now)
    assert len(store.match_scores(preferences_hash="hash-a")) == 1
    assert store.match_scores(preferences_hash="hash-b") == []


def test_digest_statistics_ignore_a_score_from_other_preferences(
    store: SqliteStore, now: datetime
) -> None:
    job_id = add_job(store, now)
    store.record_score(make_score(store, job_id, "hash-a"), now)
    assert store.digest_stats(preferences_hash="hash-a").scored_jobs == 1
    assert store.digest_stats(preferences_hash="hash-b").scored_jobs == 0


def test_a_recorded_score_carries_its_preferences_stamp(store: SqliteStore, now: datetime) -> None:
    job_id = add_job(store, now)
    store.record_score(make_score(store, job_id, "hash-a", version=3), now)
    scored = store.match_scores(preferences_hash="hash-a")[0]
    assert (scored.score.preferences_version, scored.score.preferences_hash) == (3, "hash-a")


# --- re-filter support ----------------------------------------------------


def test_all_active_jobs_is_not_limited_like_the_display_query(
    store: SqliteStore, now: datetime
) -> None:
    for index in range(60):
        add_job(
            store,
            now,
            source_job_id=str(index),
            url=f"https://x/{index}",
            title=f"Forward Deployed Engineer {index}",
        )
    assert len(store.list_jobs(limit=50)) == 50
    assert len(store.all_active_jobs()) == 60


def test_updating_a_filter_result_leaves_needs_rescore_alone(
    store: SqliteStore, now: datetime
) -> None:
    """`needs_rescore` means "the posting changed"; a preference edit is not that."""
    from career_copilot.domain.models import FilterOutcome, HardFilterResult

    job_id = add_job(store, now)
    store.clear_needs_rescore(job_id)
    store.update_filter_result(
        job_id, HardFilterResult(FilterOutcome.REJECT, reasons=("no longer wanted",))
    )
    row = store._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["filter_outcome"] == "reject"
    assert row["needs_rescore"] == 0
