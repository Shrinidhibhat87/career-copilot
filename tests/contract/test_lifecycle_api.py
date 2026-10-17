"""Decisions, viewed, and what they do to the lists: dismissed gone, new and saved on top."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import review
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import ScoreStage
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from tests.conftest import make_raw

NOW = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, Config]]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    app = FastAPI()
    app.include_router(review.router(lambda: config))
    with SqliteStore(config.db_path):
        pass
    yield TestClient(app), config


def seed(config: Config, n: int, value: int | None = 80, **overrides: object) -> int:
    """One passing posting, scored `value` unless None."""
    with SqliteStore(config.db_path) as store:
        fields: dict[str, object] = {
            "source_job_id": str(n),
            "url": f"https://x.test/{n}",
            "title": f"Data Engineer {n}",
            **overrides,
        }
        posting = to_posting(make_raw(**fields))
        job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
        if value is not None:
            content_hash = store._conn.execute(
                "SELECT content_hash FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()[0]
            store.record_score(
                Score(
                    job_id=job_id,
                    stage=ScoreStage.MATCH,
                    score=value,
                    verdict=ScoreVerdict.from_score(value),
                    rubric_version=RUBRIC_VERSION,
                    model="m",
                    prompt_hash="p",
                    content_hash=content_hash,
                ),
                NOW,
            )
        return job_id


def ids(client: TestClient, query: str = "") -> list[int]:
    return [item["id"] for item in client.get(f"/api/review?{query}").json()["items"]]


def decide(client: TestClient, job_id: int, decision: str) -> None:
    assert (
        client.post(f"/api/jobs/{job_id}/decision", json={"decision": decision}).status_code == 201
    )


def test_a_dismissed_job_leaves_every_list_and_stays_gone(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    keep, drop = seed(config, 1), seed(config, 2)
    decide(client, drop, "dismissed")
    assert ids(client) == [keep]
    assert drop not in ids(client, "includeBelow=true")
    # Undo is an explicit later event, not a mutation.
    decide(client, drop, "maybe")
    assert set(ids(client)) == {keep, drop}


def test_a_dismissed_job_stays_hidden_when_its_advert_changes(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job = seed(config, 1)
    decide(client, job, "dismissed")
    with SqliteStore(config.db_path) as store:
        posting = to_posting(
            make_raw(
                source_job_id="1",
                url="https://x.test/1",
                title="Data Engineer 1",
                description="Rewritten advert, permanent full-time role with Python.",
            )
        )
        store.upsert_job(posting, apply_hard_filters(posting), NOW)
    assert job not in ids(client, "includeBelow=true")


def test_a_dismissed_job_is_not_paid_for_again(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, 1, value=None)
    decide(client, job, "dismissed")
    with SqliteStore(config.db_path) as store:
        assert store.jobs_needing_score("m", RUBRIC_VERSION, 10) == []


def test_unknown_decision_is_422_and_unknown_job_is_404(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, 1)
    assert client.post(f"/api/jobs/{job}/decision", json={"decision": "hired"}).status_code == 422
    assert client.post("/api/jobs/999/decision", json={"decision": "saved"}).status_code == 404


def test_a_not_relevant_review_dismisses_and_a_good_fit_saves(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    no, yes = seed(config, 1), seed(config, 2)
    assert (
        client.post("/api/reviews", json={"jobId": no, "label": "not_relevant"}).json()["decision"]
        == "dismissed"
    )
    client.post("/api/reviews", json={"jobId": yes, "label": "good_fit"})
    assert ids(client) == [yes]
    [item] = client.get("/api/review").json()["items"]
    assert (item["isSaved"], item["decision"]) == (True, "saved")


def test_a_job_is_new_until_the_client_says_it_was_shown(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    a, b = seed(config, 1), seed(config, 2)
    first = client.get("/api/review").json()["items"]
    assert [item["isNew"] for item in first] == [True, True]
    # Reading does not mark anything: a reload before the cards rendered keeps the badge.
    assert [item["isNew"] for item in client.get("/api/review").json()["items"]] == [True, True]
    assert client.post("/api/jobs/viewed", json={"ids": [a, 999]}).json() == {"marked": 1}
    flags = {item["id"]: item["isNew"] for item in client.get("/api/review").json()["items"]}
    assert flags == {a: False, b: True}
    assert client.post("/api/jobs/viewed", json={"ids": [a]}).json() == {"marked": 0}


def test_viewed_rejects_a_malformed_body(api: tuple[TestClient, Config]) -> None:
    client, _ = api
    assert client.post("/api/jobs/viewed", json={"ids": "1"}).status_code == 422
    assert client.post("/api/jobs/viewed", json={"ids": [True]}).status_code == 422


def test_new_and_saved_sit_above_the_rest_each_tier_by_score(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    old_high = seed(config, 1, 95)
    old_low = seed(config, 2, 60)
    saved_low = seed(config, 3, 50)
    new_mid = seed(config, 4, 70)
    client.post("/api/jobs/viewed", json={"ids": [old_high, old_low, saved_low]})
    decide(client, saved_low, "saved")
    # new_mid (70) and saved_low (50) form the top tier by score; then the viewed ones.
    assert ids(client, "threshold=30") == [new_mid, saved_low, old_high, old_low]


def test_a_saved_job_below_the_threshold_still_shows(api: tuple[TestClient, Config]) -> None:
    client, config = api
    low = seed(config, 1, 35)
    seed(config, 2, 90)
    decide(client, low, "saved")
    assert low in ids(client, "threshold=60")


def test_the_saved_filter_narrows_the_list(api: tuple[TestClient, Config]) -> None:
    client, config = api
    saved, _other = seed(config, 1), seed(config, 2)
    decide(client, saved, "saved")
    assert ids(client, "decision=saved") == [saved]
    assert client.get("/api/review?decision=dismissed").status_code == 422


def test_a_rule_rejected_new_job_does_not_float_to_the_top(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    good = seed(config, 1, 90)
    rejected = seed(config, 2, None, title="Senior Intern (m/w/d)")
    client.post("/api/jobs/viewed", json={"ids": [good]})
    below = ids(client, "includeBelow=true")
    assert below.index(good) < below.index(rejected)


def test_the_card_reports_which_cv_scored_it(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, 1)
    assert client.get(f"/api/jobs/{job}").json()["scoredWithCv"] is None
    with SqliteStore(config.db_path) as store:
        cv = store.save_cv("cv.pdf", "Data engineer.", NOW).cv
        store._conn.execute("UPDATE scores SET cv_sha256 = ? WHERE job_id = ?", (cv.sha256, job))
        store._conn.commit()
    assert client.get(f"/api/jobs/{job}").json()["scoredWithCv"] == 1


def test_deciding_marks_the_job_viewed(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, 1)
    decide(client, job, "maybe")
    assert client.get(f"/api/jobs/{job}").json()["isNew"] is False


def test_the_digest_leaves_out_dismissed_jobs(api: tuple[TestClient, Config]) -> None:
    client, config = api
    keep, drop = seed(config, 1), seed(config, 2)
    decide(client, drop, "dismissed")
    with SqliteStore(config.db_path) as store:
        # The digest's source of truth, which filters on filter_outcome = 'pass'.
        listed = [s.job.id for s in store.match_scores(limit=None)]
    assert listed == [keep]


def test_an_applied_job_stays_listed_with_its_badge_below_new_and_saved(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    applied = seed(config, 1, 95)
    saved = seed(config, 2, 50)
    new = seed(config, 3, 60)
    client.post("/api/jobs/viewed", json={"ids": [saved]})
    decide(client, saved, "saved")
    decide(client, applied, "applied")
    assert ids(client) == [new, saved, applied]
    item = next(i for i in client.get("/api/review").json()["items"] if i["id"] == applied)
    assert (item["decision"], item["isApplied"], item["isSaved"]) == ("applied", True, False)


def test_an_applied_job_below_the_threshold_still_shows(api: tuple[TestClient, Config]) -> None:
    client, config = api
    low = seed(config, 1, 35)
    decide(client, low, "applied")
    assert low in ids(client, "threshold=60")


def test_the_applied_filter_narrows_the_list(api: tuple[TestClient, Config]) -> None:
    client, config = api
    applied, saved = seed(config, 1), seed(config, 2)
    decide(client, applied, "applied")
    decide(client, saved, "saved")
    assert ids(client, "decision=applied") == [applied]


def test_an_applied_job_is_not_scored_again(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, 1, value=None)
    decide(client, job, "applied")
    with SqliteStore(config.db_path) as store:
        assert store.jobs_needing_score("m", RUBRIC_VERSION, 10) == []


def undo(client: TestClient, job_id: int) -> dict[str, object]:
    response = client.post(f"/api/jobs/{job_id}/undo-applied")
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def test_undo_applied_on_an_untouched_job_restores_no_decision(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job = seed(config, 1)
    before = client.get(f"/api/jobs/{job}").json()
    decide(client, job, "applied")
    assert undo(client, job) == {"decision": None}
    after = client.get(f"/api/jobs/{job}").json()
    for key in ("decision", "isApplied", "isSaved", "label"):
        assert after[key] == before[key]
    row = next(i for i in client.get("/api/review").json()["items"] if i["id"] == job)
    assert (row["decision"], row["isSaved"], row["isApplied"]) == (None, False, False)
    # Append-only: the undo is a new row, not an edit.
    with SqliteStore(config.db_path) as store:
        rows = store._conn.execute(
            "SELECT decision FROM job_decisions WHERE job_id = ? ORDER BY id", (job,)
        ).fetchall()
        assert len(rows) == 2
        assert store.jobs_needing_score("m2", RUBRIC_VERSION, 10)[0].id == job


def test_undo_applied_restores_the_decision_before_it(api: tuple[TestClient, Config]) -> None:
    client, config = api
    saved, maybe = seed(config, 1), seed(config, 2)
    decide(client, saved, "saved")
    decide(client, maybe, "maybe")
    for job, was in ((saved, "saved"), (maybe, "maybe")):
        decide(client, job, "applied")
        assert undo(client, job) == {"decision": was}
        assert client.get(f"/api/jobs/{job}").json()["decision"] == was


def test_apply_undo_twice_returns_to_the_same_state(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, 1)
    decide(client, job, "saved")
    for _ in range(2):
        decide(client, job, "applied")
        assert undo(client, job) == {"decision": "saved"}


def test_undo_applied_on_a_job_that_is_not_applied_is_409(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job = seed(config, 1)
    assert client.post(f"/api/jobs/{job}/undo-applied").status_code == 409
    assert client.post("/api/jobs/999/undo-applied").status_code == 404


def test_a_review_row_carries_its_notion_index(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, 1)
    with SqliteStore(config.db_path) as store:
        store.record_application(store.next_application_index(), job, "page-1", NOW)
    [row] = client.get("/api/review").json()["items"]
    assert row["notionIndex"] == 1


def test_the_card_reports_its_notion_index(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, 1)
    assert client.get(f"/api/jobs/{job}").json()["notionIndex"] is None
    with SqliteStore(config.db_path) as store:
        store.record_application(store.next_application_index(), job, "page-1", NOW)
    assert client.get(f"/api/jobs/{job}").json()["notionIndex"] == 1


def test_a_dismissal_can_carry_a_reason(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job_id = seed(config, 1)

    response = client.post(
        f"/api/jobs/{job_id}/decision", json={"decision": "dismissed", "reason": "seniority"}
    )

    assert response.status_code == 201
    with SqliteStore(config.db_path) as store:
        assert [(s.decision, s.reason) for s in store.decision_signals()] == [
            ("dismissed", "seniority")
        ]


def test_a_reason_must_be_known_and_belongs_to_a_dismissal(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job_id = seed(config, 1)
    url = f"/api/jobs/{job_id}/decision"

    assert client.post(url, json={"decision": "dismissed", "reason": "boring"}).status_code == 422
    assert client.post(url, json={"decision": "saved", "reason": "location"}).status_code == 422
