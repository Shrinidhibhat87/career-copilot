"""`POST /api/jobs/{id}/notion`: Applied always saves; Notion adds a row once, never blocks."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import applications, review
from career_copilot.adapters.notion_applications import NotionCannotWrite, NotionPageMissing
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config, NotionSpec
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.ports import ApplicationEntry, NotionResult
from tests.conftest import make_raw

NOW = datetime(2026, 10, 8, tzinfo=UTC)


@dataclass
class FakeLog:
    entries: list[ApplicationEntry] = field(default_factory=list)
    saved: list[tuple[str, str] | None] = field(default_factory=list)
    fail: Exception | None = None

    def factory(self, config: Config, saved: tuple[str, str] | None) -> FakeLog:
        self.saved.append(saved)
        return self

    def record(self, entry: ApplicationEntry) -> NotionResult:
        if self.fail is not None:
            raise self.fail
        self.entries.append(entry)
        return NotionResult(f"row-{entry.index}", "db-1", "parent-1", created=True)


@pytest.fixture
def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Config, FakeLog]]:
    monkeypatch.setenv("NOTION_API_KEY", "secret")
    config = Config(db_path=str(tmp_path / "jobs.db"))
    fake = FakeLog()
    app = FastAPI()
    app.include_router(review.router(lambda: config))
    app.include_router(applications.router(lambda: config, log_factory=fake.factory))
    with SqliteStore(config.db_path):
        pass
    yield TestClient(app), config, fake


def seed(config: Config, n: int) -> int:
    with SqliteStore(config.db_path) as store:
        posting = to_posting(
            make_raw(source_job_id=str(n), url=f"https://x.test/{n}", title=f"Data Engineer {n}")
        )
        job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
        return job_id


def apply(client: TestClient, job_id: int) -> None:
    response = client.post(f"/api/jobs/{job_id}/decision", json={"decision": "applied"})
    assert response.status_code == 201


def test_index_values_are_1_2_3_and_a_repeat_adds_no_row(
    api: tuple[TestClient, Config, FakeLog],
) -> None:
    client, config, fake = api
    jobs = [seed(config, n) for n in (1, 2, 3)]
    answers = [client.post(f"/api/jobs/{job}/notion").json() for job in jobs]
    assert answers == [{"status": "added", "index": i} for i in (1, 2, 3)]
    assert [e.index for e in fake.entries] == [1, 2, 3]

    again = client.post(f"/api/jobs/{jobs[0]}/notion")
    assert again.status_code == 200
    assert again.json() == {"status": "already", "index": 1}
    assert len(fake.entries) == 3
    assert client.get(f"/api/jobs/{jobs[0]}").json()["notionIndex"] == 1


def test_the_entry_is_built_from_the_job(api: tuple[TestClient, Config, FakeLog]) -> None:
    client, config, fake = api
    job = seed(config, 1)
    client.post(f"/api/jobs/{job}/notion")
    [entry] = fake.entries
    assert (entry.role, entry.status, entry.job_url, entry.comments) == (
        "Data Engineer 1",
        "Applied",
        "https://x.test/1",
        "",
    )
    assert entry.pilot_score is None  # not scored yet


def test_the_found_database_is_saved_and_passed_to_the_next_call(
    api: tuple[TestClient, Config, FakeLog],
) -> None:
    client, config, fake = api
    a, b = seed(config, 1), seed(config, 2)
    client.post(f"/api/jobs/{a}/notion")
    client.post(f"/api/jobs/{b}/notion")
    assert fake.saved == [None, ("db-1", "parent-1")]


def test_no_key_still_saves_applied_and_notion_is_409(
    api: tuple[TestClient, Config, FakeLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, fake = api
    monkeypatch.delenv("NOTION_API_KEY")
    job = seed(config, 1)
    apply(client, job)
    response = client.post(f"/api/jobs/{job}/notion")
    assert response.status_code == 409
    assert "NOTION_API_KEY" in response.json()["detail"]["errors"]["notion"]
    assert fake.entries == []
    assert client.get(f"/api/jobs/{job}").json()["decision"] == "applied"


def test_a_missing_parent_page_is_409_with_the_reason(
    api: tuple[TestClient, Config, FakeLog],
) -> None:
    client, config, fake = api
    fake.fail = NotionPageMissing("Share a page titled 'Career Copilot' with your integration")
    job = seed(config, 1)
    response = client.post(f"/api/jobs/{job}/notion")
    assert response.status_code == 409
    assert "Career Copilot" in response.json()["detail"]["errors"]["notion"]


def test_a_write_failure_is_502_keeps_applied_and_records_nothing(
    api: tuple[TestClient, Config, FakeLog],
) -> None:
    client, config, fake = api
    fake.fail = NotionCannotWrite("tick Insert content")
    job = seed(config, 1)
    apply(client, job)
    response = client.post(f"/api/jobs/{job}/notion")
    assert response.status_code == 502
    assert "Insert content" in response.json()["detail"]["errors"]["notion"]
    card = client.get(f"/api/jobs/{job}").json()
    assert (card["decision"], card["notionIndex"]) == ("applied", None)


def test_an_unknown_job_is_404(api: tuple[TestClient, Config, FakeLog]) -> None:
    client, _, _ = api
    assert client.post("/api/jobs/999/notion").status_code == 404


def test_the_titles_come_from_config() -> None:
    spec = Config().notion
    assert spec == NotionSpec()
    assert (spec.applications_parent_title, spec.applications_database_title) == (
        "Career Copilot",
        "Applications",
    )
