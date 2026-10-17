"""Versioned CV storage: identity by normalized text, immutable history, no staleness."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.cv import cv_sha256
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import ScoreStage
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from career_copilot.ports import ResumeMissingError
from career_copilot.usecases.cv import ensure_current_cv
from tests.conftest import make_raw


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as s:
        yield s


def test_first_upload_is_version_one_and_current(store: SqliteStore, now: datetime) -> None:
    saved = store.save_cv("cv.pdf", "Data engineer. Python.", now)
    assert saved.changed is True
    assert (saved.cv.version, saved.cv.current) == (1, True)
    assert store.current_cv() == saved.cv


def test_identical_text_is_a_no_op(store: SqliteStore, now: datetime) -> None:
    store.save_cv("cv.pdf", "Data engineer. Python.", now)
    again = store.save_cv("cv-export.pdf", "Data engineer.\n\n  Python.", now + timedelta(days=1))
    assert again.changed is False
    assert again.cv.version == 1
    assert len(store.list_cvs()) == 1


def test_different_text_is_the_next_version_and_history_is_kept(
    store: SqliteStore, now: datetime
) -> None:
    store.save_cv("cv.pdf", "Data engineer.", now)
    second = store.save_cv("cv.pdf", "Data engineer. Kafka.", now + timedelta(days=1))
    assert second.changed is True and second.cv.version == 2
    versions = store.list_cvs()
    assert [(v.version, v.current) for v in versions] == [(2, True), (1, False)]


def test_reuploading_an_old_version_makes_it_current_without_a_new_row(
    store: SqliteStore, now: datetime
) -> None:
    store.save_cv("cv.pdf", "First.", now)
    store.save_cv("cv.pdf", "Second.", now)
    reverted = store.save_cv("cv.pdf", "First.", now)
    assert reverted.changed is True and reverted.cv.version == 1
    current = store.current_cv()
    assert current is not None and current.version == 1
    assert len(store.list_cvs()) == 2


def test_the_cv_survives_reopening_the_database(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "jobs.db"
    with SqliteStore(path) as first:
        first.save_cv("cv.pdf", "Data engineer.", now)
    with SqliteStore(path) as second:
        cv = second.current_cv()
    assert cv is not None and cv.text == "Data engineer." and cv.sha256 == cv_sha256(cv.text)


def test_deleting_removes_every_version(store: SqliteStore, now: datetime) -> None:
    store.save_cv("cv.pdf", "One.", now)
    store.save_cv("cv.pdf", "Two.", now)
    assert store.delete_cvs() == 2
    assert store.current_cv() is None
    assert store.save_cv("cv.pdf", "One.", now).cv.version == 1


def _scored_job(store: SqliteStore, now: datetime, cv_sha: str | None) -> int:
    posting = to_posting(make_raw(), store.company_aliases())
    job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), now)
    row = store._conn.execute("SELECT content_hash FROM jobs WHERE id = ?", (job_id,)).fetchone()
    score = Score(
        job_id=job_id,
        stage=ScoreStage.MATCH,
        score=80,
        verdict=ScoreVerdict.STRONG,
        rubric_version=RUBRIC_VERSION,
        model="m",
        prompt_hash="p",
        content_hash=row["content_hash"],
        cv_sha256=cv_sha,
    )
    store.record_score(score, now)
    return job_id


def test_score_records_which_cv_produced_it(store: SqliteStore, now: datetime) -> None:
    sha = store.save_cv("cv.pdf", "One.", now).cv.sha256
    job_id = _scored_job(store, now, sha)
    row = store._conn.execute("SELECT cv_sha256 FROM scores WHERE job_id = ?", (job_id,)).fetchone()
    assert row["cv_sha256"] == sha


def test_a_new_cv_does_not_make_existing_scores_stale(store: SqliteStore, now: datetime) -> None:
    first = store.save_cv("cv.pdf", "One.", now).cv
    _scored_job(store, now, first.sha256)
    assert store.jobs_needing_score("m", RUBRIC_VERSION, 10) == []
    store.save_cv("cv.pdf", "Two, quite different.", now + timedelta(days=1))
    assert store.jobs_needing_score("m", RUBRIC_VERSION, 10) == []


def test_ensure_current_cv_without_a_stored_cv_is_an_error(store: SqliteStore) -> None:
    with pytest.raises(ResumeMissingError):
        ensure_current_cv(store)


def test_ensure_current_cv_returns_the_stored_cv(store: SqliteStore, now: datetime) -> None:
    store.save_cv("upload.pdf", "From the upload.", now)
    assert ensure_current_cv(store).text == "From the upload."
