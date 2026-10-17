"""The recycle bin over HTTP: restore, delete forever, and a second dismissal's fresh clock."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import recycle_bin, review
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from tests.contract.test_lifecycle_api import decide, ids, seed

START = datetime(2026, 9, 6, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Config, Clock]]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    clock = Clock()
    monkeypatch.setattr(review, "now", clock)
    monkeypatch.setattr(recycle_bin, "now", clock)
    app = FastAPI()
    app.include_router(review.router(lambda: config))
    app.include_router(recycle_bin.router(lambda: config))
    with SqliteStore(config.db_path):
        pass
    yield TestClient(app), config, clock


def bin_ids(client: TestClient) -> list[int]:
    return [item["jobId"] for item in client.get("/api/bin").json()["items"]]


def test_the_bin_lists_a_dismissed_job_with_days_left(
    api: tuple[TestClient, Config, Clock],
) -> None:
    client, config, clock = api
    job = seed(config, 1)
    decide(client, job, "dismissed")
    clock.at = START + timedelta(days=2)
    body = client.get("/api/bin").json()
    assert body["days"] == 7
    [item] = body["items"]
    assert item["jobId"] == job
    assert item["daysLeft"] == 5
    assert set(item) == {"jobId", "title", "company", "dismissedAt", "daysLeft"}
    assert client.get("/api/review").json()["binCount"] == 1


def test_restore_lists_the_job_again(api: tuple[TestClient, Config, Clock]) -> None:
    client, config, _ = api
    job = seed(config, 1)
    decide(client, job, "dismissed")
    assert client.post(f"/api/jobs/{job}/restore").status_code == 200
    assert job in ids(client)
    assert bin_ids(client) == []
    [row] = client.get("/api/review").json()["items"]
    assert row["decision"] is None


def test_a_restored_job_is_scored_again(api: tuple[TestClient, Config, Clock]) -> None:
    client, config, _ = api
    job = seed(config, 1, value=None)
    decide(client, job, "dismissed")
    client.post(f"/api/jobs/{job}/restore")
    with SqliteStore(config.db_path) as store:
        assert [stored.id for stored in store.jobs_needing_score("m", 1, 10)] == [job]


def test_restore_of_a_cleaned_or_unknown_job_is_404(
    api: tuple[TestClient, Config, Clock],
) -> None:
    client, config, clock = api
    job = seed(config, 1)
    decide(client, job, "dismissed")
    clock.at = START + timedelta(days=7)
    assert bin_ids(client) == []
    assert client.post(f"/api/jobs/{job}/restore").status_code == 404
    assert client.post("/api/jobs/999/restore").status_code == 404


def test_delete_forever_cleans_the_job_and_it_leaves_the_bin(
    api: tuple[TestClient, Config, Clock],
) -> None:
    client, config, _ = api
    job = seed(config, 1)
    decide(client, job, "dismissed")
    assert client.delete(f"/api/bin/{job}").status_code == 204
    assert bin_ids(client) == []
    with SqliteStore(config.db_path) as store:
        row = store._conn.execute(
            "SELECT description, tombstoned_at FROM jobs WHERE id = ?", (job,)
        ).fetchone()
    assert row["description"] == ""
    assert row["tombstoned_at"] is not None
    assert client.delete(f"/api/bin/{job}").status_code == 404
    assert job not in ids(client, "includeBelow=true")


def test_a_second_dismissal_gets_a_fresh_seven_days(
    api: tuple[TestClient, Config, Clock],
) -> None:
    client, config, clock = api
    job = seed(config, 1)
    decide(client, job, "dismissed")
    clock.at = START + timedelta(days=5)
    client.post(f"/api/jobs/{job}/restore")
    decide(client, job, "dismissed")
    clock.at = START + timedelta(days=6)
    [item] = client.get("/api/bin").json()["items"]
    assert item["daysLeft"] == 6
    clock.at = START + timedelta(days=11)
    assert bin_ids(client) == [job]
    clock.at = START + timedelta(days=12)
    assert bin_ids(client) == []


def test_a_cleaned_job_takes_no_decision_and_restored_is_not_a_decision_route(
    api: tuple[TestClient, Config, Clock],
) -> None:
    client, config, _ = api
    job = seed(config, 1)
    decide(client, job, "dismissed")
    # Restore has its own route, which checks the job is in the bin.
    assert (
        client.post(f"/api/jobs/{job}/decision", json={"decision": "restored"}).status_code == 422
    )
    client.delete(f"/api/bin/{job}")
    assert client.post(f"/api/jobs/{job}/decision", json={"decision": "saved"}).status_code == 404
    assert job not in ids(client, "includeBelow=true")


def test_a_label_cannot_bring_a_cleaned_job_back(api: tuple[TestClient, Config, Clock]) -> None:
    client, config, _ = api
    job = seed(config, 1)
    decide(client, job, "dismissed")
    client.delete(f"/api/bin/{job}")
    assert client.post("/api/reviews", json={"jobId": job, "label": "good_fit"}).status_code == 404
    assert job not in ids(client, "includeBelow=true")
