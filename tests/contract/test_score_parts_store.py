"""Stored score parts: the migration, the version-aware read, and which scores are stale."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from career_copilot.adapters.store import migrations
from career_copilot.adapters.store.migrations import migrate
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.scoring import RUBRIC_VERSION
from tests.contract.test_review_api import api, score, seed

__all__ = ["api"]  # the fixture, re-exported so pytest finds it here

PARTS: dict[str, dict[str, Any]] = {
    "skills": {"points": 30, "reason": "Python and LangGraph match."},
    "seniority": {"points": 20, "reason": "Senior scope."},
    "domain": {"points": 15, "reason": "Adjacent domain."},
    "preferences": {"points": 10, "reason": "Berlin, hybrid."},
}


def test_rubric_version_is_three() -> None:
    assert RUBRIC_VERSION == 3
    assert Config().scoring.rubric_version == 3


def test_migration_keeps_existing_rows_and_leaves_parts_null(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for version, sql in migrations.MIGRATIONS:
        if version <= 17:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    conn.commit()
    migrate(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(scores)")}
    assert "parts" in columns
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 33


def test_a_version_one_job_is_still_listed_with_null_parts(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job = seed(config, source_job_id="1", url="https://x.test/1")
    score(config, job, 70, rubric_version=1)

    items = client.get("/api/review?threshold=60&includeBelow=true").json()["items"]
    assert [item["id"] for item in items] == [job]
    assert items[0]["parts"] is None
    assert items[0]["score"] == 70
    card = client.get(f"/api/jobs/{job}").json()
    assert card["parts"] is None
    assert card["rubricVersion"] == 1


def test_the_list_and_card_prefer_the_highest_rubric_version(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job = seed(config, source_job_id="1", url="https://x.test/1")
    score(config, job, 70, rubric_version=1)
    score(config, job, 75, rubric_version=2, parts=PARTS)
    # A later write under the old rubric must not win over the newer one.
    score(config, job, 60, rubric_version=1, model="other-model")

    items = client.get("/api/review?threshold=60&includeBelow=true").json()["items"]
    assert [(i["score"], i["parts"]) for i in items] == [(75, PARTS)]
    card = client.get(f"/api/jobs/{job}").json()
    assert (card["matchScore"], card["parts"], card["rubricVersion"]) == (75, PARTS, 2)


def test_parts_round_trip_through_the_store(api: tuple[TestClient, Config]) -> None:
    _, config = api
    job = seed(config, source_job_id="1", url="https://x.test/1")
    score(config, job, 75, rubric_version=2, parts=PARTS)
    with SqliteStore(config.db_path) as store:
        detail = store.job_detail(job)
        scored = store.match_scores(limit=10)
    assert detail is not None and detail["parts"] == PARTS
    assert scored[0].score.parts == PARTS
    assert sum(p["points"] for p in PARTS.values()) == scored[0].score.score


def test_needs_scoring_is_true_for_an_older_version_and_false_for_the_current_version(
    api: tuple[TestClient, Config],
) -> None:
    _, config = api
    old = seed(config, source_job_id="1", url="https://x.test/1")
    new = seed(config, source_job_id="2", url="https://x.test/2", title="Data Engineer B")
    score(config, old, 70, rubric_version=1)
    score(config, new, 75, rubric_version=RUBRIC_VERSION, parts=PARTS)
    with SqliteStore(config.db_path) as store:
        needing = [j.id for j in store.jobs_needing_score("test-model", RUBRIC_VERSION)]
    assert needing == [old]
