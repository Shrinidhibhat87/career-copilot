"""`/api/pipeline`: stages, notes, hide, settings and the Notion mirror (spec 021)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import pipeline, review
from career_copilot.adapters.sources.notion import NotionError
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.ports import ApplicationEntry, NotionResult
from tests.conftest import make_raw

NOW = datetime(2026, 11, 11, tzinfo=UTC)


@dataclass
class FakeLog:
    updates: list[tuple[str, str, str]] = field(default_factory=list)
    fail: Exception | None = None

    def factory(self, config: Config, saved: tuple[str, str] | None) -> FakeLog:
        return self

    def record(self, entry: ApplicationEntry) -> NotionResult:
        raise AssertionError("the board never adds a row")

    def update(self, page_id: str, status: str, comments: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.updates.append((page_id, status, comments))


@pytest.fixture
def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Config, FakeLog]]:
    monkeypatch.setenv("NOTION_API_KEY", "secret-key")
    config = Config(db_path=str(tmp_path / "jobs.db"))
    fake = FakeLog()
    app = FastAPI()
    app.include_router(review.router(lambda: config))
    app.include_router(pipeline.router(lambda: config, log_factory=fake.factory))
    with SqliteStore(config.db_path):
        pass
    yield TestClient(app), config, fake


def applied_job(client: TestClient, config: Config, n: int = 1, notion: bool = False) -> int:
    with SqliteStore(config.db_path) as store:
        posting = to_posting(
            make_raw(source_job_id=str(n), url=f"https://x.test/{n}", title=f"Data Engineer {n}")
        )
        job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
        if notion:
            store.record_application(1, job_id, "page-1", NOW)
    assert (
        client.post(f"/api/jobs/{job_id}/decision", json={"decision": "applied"}).status_code == 201
    )
    return job_id


def test_an_applied_job_lands_in_the_applied_column(
    api: tuple[TestClient, Config, FakeLog],
) -> None:
    client, config, _ = api
    job = applied_job(client, config)
    board = client.get("/api/pipeline").json()
    assert [c["jobId"] for c in board["applied"]] == [job]
    assert board["staleDays"] == 14


def test_stage_moves_and_closed_splits_into_offers_and_folded(
    api: tuple[TestClient, Config, FakeLog],
) -> None:
    client, config, _ = api
    a, b, c = (applied_job(client, config, n) for n in (1, 2, 3))
    client.post(f"/api/pipeline/{a}/stage", json={"stage": "interviewing"})
    client.post(f"/api/pipeline/{b}/stage", json={"stage": "offer"})
    client.post(f"/api/pipeline/{c}/stage", json={"stage": "rejected", "note": "no budget"})
    board = client.get("/api/pipeline").json()
    assert [x["jobId"] for x in board["interviewing"]] == [a]
    assert [x["jobId"] for x in board["closed"]["offers"]] == [b]
    assert [x["jobId"] for x in board["closed"]["folded"]] == [c]
    assert [t["stage"] for t in client.get(f"/api/pipeline/{c}/timeline").json()] == [
        "applied",
        "rejected",
    ]


def test_bad_stage_and_unapplied_job_are_refused(api: tuple[TestClient, Config, FakeLog]) -> None:
    client, config, _ = api
    job = applied_job(client, config)
    assert client.post(f"/api/pipeline/{job}/stage", json={"stage": "ghosted"}).status_code == 422
    assert client.post("/api/pipeline/999/stage", json={"stage": "offer"}).status_code == 404
    assert client.put(f"/api/pipeline/{job}/note", json={"note": "x" * 2001}).status_code == 422


def test_hide_and_unhide(api: tuple[TestClient, Config, FakeLog]) -> None:
    client, config, _ = api
    job = applied_job(client, config)
    client.post(f"/api/pipeline/{job}/hide")
    board = client.get("/api/pipeline").json()
    assert board["applied"] == [] and [c["jobId"] for c in board["hidden"]] == [job]
    client.post(f"/api/pipeline/{job}/unhide")
    assert [c["jobId"] for c in client.get("/api/pipeline").json()["applied"]] == [job]


def test_undo_applied_takes_the_card_off_the_board(api: tuple[TestClient, Config, FakeLog]) -> None:
    client, config, _ = api
    job = applied_job(client, config)
    assert client.post(f"/api/jobs/{job}/undo-applied").status_code == 200
    board = client.get("/api/pipeline").json()
    assert board["applied"] == []


def test_settings_round_trip_and_bounds(api: tuple[TestClient, Config, FakeLog]) -> None:
    client, _, _ = api
    assert client.put("/api/pipeline/settings", json={"staleDays": 30}).json() == {"staleDays": 30}
    assert client.get("/api/pipeline/settings").json() == {"staleDays": 30}
    assert client.put("/api/pipeline/settings", json={"staleDays": 0}).status_code == 422
    assert client.put("/api/pipeline/settings", json={"staleDays": 366}).status_code == 422


def test_a_job_in_notion_gets_its_status_and_note_updated(
    api: tuple[TestClient, Config, FakeLog],
) -> None:
    client, config, fake = api
    job = applied_job(client, config, notion=True)
    client.put(f"/api/pipeline/{job}/note", json={"note": "Call Anna"})
    client.post(f"/api/pipeline/{job}/stage", json={"stage": "interviewing"})
    assert fake.updates == [
        ("page-1", "Applied", "Call Anna"),
        ("page-1", "Interviewing", "Call Anna"),
    ]


def test_a_job_not_in_notion_makes_no_call(api: tuple[TestClient, Config, FakeLog]) -> None:
    client, config, fake = api
    job = applied_job(client, config)
    client.post(f"/api/pipeline/{job}/stage", json={"stage": "offer"})
    assert fake.updates == []


def test_a_notion_failure_keeps_the_move_and_a_later_change_clears_it(
    api: tuple[TestClient, Config, FakeLog],
) -> None:
    client, config, fake = api
    job = applied_job(client, config, notion=True)
    fake.fail = NotionError("Notion unreachable (ConnectError)")
    moved = client.post(f"/api/pipeline/{job}/stage", json={"stage": "interviewing"})
    assert moved.status_code == 200
    assert moved.json()["stage"] == "interviewing"
    assert moved.json()["notionError"] == "Notion unreachable (ConnectError)"
    assert "secret-key" not in moved.text
    fake.fail = None
    again = client.post(f"/api/pipeline/{job}/stage", json={"stage": "offer"})
    assert again.json()["notionError"] is None
    assert fake.updates[-1] == ("page-1", "Offer", "")


def test_a_missing_key_is_reported_on_the_card(
    api: tuple[TestClient, Config, FakeLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, fake = api
    job = applied_job(client, config, notion=True)
    monkeypatch.delenv("NOTION_API_KEY")
    moved = client.post(f"/api/pipeline/{job}/stage", json={"stage": "offer"})
    assert "NOTION_API_KEY" in moved.json()["notionError"]
    assert fake.updates == []
