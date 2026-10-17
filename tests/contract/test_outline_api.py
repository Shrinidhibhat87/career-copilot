"""HTTP contract for a card's outline: read once, kept, and honest when the model fails."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import outline
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.ports import LLMClient, LLMResponse
from tests.conftest import make_raw

NOW = datetime(2026, 9, 6, tzinfo=UTC)
DESCRIPTION = (
    "KINEXON Sports is a global pioneer in tracking technology. "
    "YOUR TASKS Design and build backend services for our positioning system. "
    "YOUR QUALIFICATIONS Solid experience with Go, Python or C++."
)


class ScriptedLLM:
    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = data
        self.calls = 0

    def complete(
        self,
        prompt: str,
        schema: object,
        system: str | None = None,
        validator: object = None,
    ) -> LLMResponse:
        self.calls += 1
        if self.data is None:
            raise ConnectionError("model is down")
        return LLMResponse(data=self.data, model="test-model")


GOOD: dict[str, object] = {
    "responsibilities": ["Design and build backend services for our positioning system."],
    "requirements": ["Solid experience with Go, Python or C++."],
}


def build(tmp_path: Path, llm: ScriptedLLM) -> tuple[TestClient, Config, int]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    app = FastAPI()

    def factory(_: Config) -> LLMClient:
        return llm

    app.include_router(outline.router(lambda: config, factory))
    with SqliteStore(config.db_path) as store:
        posting = to_posting(make_raw(description=DESCRIPTION))
        job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
    return TestClient(app), config, job_id


@pytest.fixture
def good(tmp_path: Path) -> Iterator[tuple[TestClient, ScriptedLLM, int]]:
    llm = ScriptedLLM(GOOD)
    client, _, job_id = build(tmp_path, llm)
    yield client, llm, job_id


def test_the_first_open_reads_the_posting_and_the_second_does_not(
    good: tuple[TestClient, ScriptedLLM, int],
) -> None:
    client, llm, job_id = good
    first = client.post(f"/api/jobs/{job_id}/outline").json()
    second = client.post(f"/api/jobs/{job_id}/outline").json()

    assert first == {"status": "ok", **GOOD}
    assert second == first
    assert llm.calls == 1


def test_a_failing_model_is_reported_and_tried_again_next_time(tmp_path: Path) -> None:
    llm = ScriptedLLM(None)
    client, _, job_id = build(tmp_path, llm)

    assert client.post(f"/api/jobs/{job_id}/outline").json() == {"status": "unavailable"}
    assert client.post(f"/api/jobs/{job_id}/outline").json() == {"status": "unavailable"}
    assert llm.calls == 2


def test_an_answer_that_is_not_in_the_posting_is_unavailable_and_not_kept(
    tmp_path: Path,
) -> None:
    llm = ScriptedLLM(
        {"responsibilities": ["Invented duty that is not there."], "requirements": []}
    )
    client, config, job_id = build(tmp_path, llm)

    assert client.post(f"/api/jobs/{job_id}/outline").json() == {"status": "unavailable"}
    with SqliteStore(config.db_path) as store:
        assert store.get_outline(job_id) is None


def test_an_unknown_job_is_404(good: tuple[TestClient, ScriptedLLM, int]) -> None:
    client, llm, _ = good
    assert client.post("/api/jobs/9999/outline").status_code == 404
    assert llm.calls == 0
