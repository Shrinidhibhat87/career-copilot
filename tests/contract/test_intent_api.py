"""HTTP contract for the intent log: append, list, and archive by DELETE."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import intent
from career_copilot.config import Config
from career_copilot.domain.intent import CONDENSE_AT_WORDS, MAX_ENTRY_CHARS


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    app = FastAPI()
    app.include_router(intent.router(lambda: config))
    yield TestClient(app)


def test_post_then_list(client: TestClient) -> None:
    created = client.post("/api/intent", json={"text": "  FDE roles in Berlin  "})
    assert created.status_code == 201
    body = created.json()
    assert body["text"] == "FDE roles in Berlin"
    assert body["kind"] == "entry"
    assert "createdAt" in body
    listed = client.get("/api/intent").json()
    assert [e["id"] for e in listed["entries"]] == [body["id"]]
    assert listed["activeWords"] == 4
    assert listed["condenseAt"] == CONDENSE_AT_WORDS


def test_empty_log_lists_nothing(client: TestClient) -> None:
    assert client.get("/api/intent").json() == {
        "entries": [],
        "activeWords": 0,
        "condenseAt": CONDENSE_AT_WORDS,
    }


@pytest.mark.parametrize("text", ["", "   ", None, 7])
def test_blank_or_non_text_is_refused(client: TestClient, text: object) -> None:
    response = client.post("/api/intent", json={"text": text})
    assert response.status_code == 422
    assert "text" in response.json()["detail"]["errors"]


def test_too_long_is_refused_and_nothing_is_stored(client: TestClient) -> None:
    response = client.post("/api/intent", json={"text": "x" * (MAX_ENTRY_CHARS + 1)})
    assert response.status_code == 422
    assert "text" in response.json()["detail"]["errors"]
    assert client.get("/api/intent").json()["entries"] == []


def test_exactly_the_limit_is_accepted(client: TestClient) -> None:
    response = client.post("/api/intent", json={"text": "x" * MAX_ENTRY_CHARS})
    assert response.status_code == 201


def test_delete_archives_and_second_delete_is_404(client: TestClient) -> None:
    entry_id = client.post("/api/intent", json={"text": "remove me"}).json()["id"]
    assert client.delete(f"/api/intent/{entry_id}").status_code == 204
    assert client.get("/api/intent").json()["entries"] == []
    assert client.delete(f"/api/intent/{entry_id}").status_code == 404
