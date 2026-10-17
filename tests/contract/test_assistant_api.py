"""HTTP contract for the feedback agent: what is stored, refused and kept through a failure."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from career_copilot.adapters.api import assistant
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.assistant.agent import build_assistant
from career_copilot.config import BackendSpec, Config, ModelsSpec, RoleSpec


def local_config(tmp_path: Path) -> Config:
    models = ModelsSpec(
        roles={"triage": RoleSpec(backend="ollama", model="qwen3:4b-instruct")},
    )
    return Config(db_path=str(tmp_path / "jobs.db"), models=models)


def remote_config(tmp_path: Path) -> Config:
    models = ModelsSpec(
        backends={"or": BackendSpec(kind="openrouter")},
        roles={"triage": RoleSpec(backend="or", model="google/gemini-2.5-flash")},
    )
    return Config(db_path=str(tmp_path / "jobs.db"), models=models)


def make_client(config: Config, respond: Any) -> TestClient:
    app = FastAPI()
    app.include_router(
        assistant.router(
            lambda: config,
            agent_factory=lambda _c, rec: build_assistant(FunctionModel(respond), rec),
            redact_factory=lambda _c: None,
            on_refresh=lambda _c: {"started": False, "reason": "upload a CV first"},
        )
    )
    return TestClient(app)


def answers(text: str) -> Any:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return respond


def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise RuntimeError("backend down")


def rows(config: Config) -> list[tuple[str, str]]:
    with SqliteStore(config.db_path) as store:
        return [(e.role, e.text) for e in store.recent_feedback(20)]


BODY = {"message": "hello", "history": [], "view": {"limit": 10, "includeBelow": False}}


@pytest.fixture
def config(tmp_path: Path) -> Iterator[Config]:
    yield local_config(tmp_path)


def test_a_reply_is_returned_and_both_messages_are_stored(config: Config) -> None:
    response = make_client(config, answers("Hi!")).post("/api/assistant", json=BODY)
    assert response.status_code == 200
    assert response.json() == {"text": "Hi!", "actions": [], "busy": False}
    assert rows(config) == [("user", "hello"), ("assistant", "Hi!")]


def test_a_failing_model_is_200_and_still_saves_the_note(config: Config) -> None:
    response = make_client(config, broken).post("/api/assistant", json=BODY)
    assert response.status_code == 200
    assert response.json()["actions"] == []
    assert [role for role, _ in rows(config)] == ["user", "assistant"]
    assert rows(config)[0] == ("user", "hello")


def test_a_non_local_role_is_409_and_nothing_is_saved(tmp_path: Path) -> None:
    config = remote_config(tmp_path)
    response = make_client(config, answers("never")).post("/api/assistant", json=BODY)
    assert response.status_code == 409
    assert "local model" in response.json()["detail"]["errors"]["model"]
    assert rows(config) == []


@pytest.mark.parametrize("message", ["", "   ", "x" * 2001])
def test_an_empty_or_long_message_is_422(config: Config, message: str) -> None:
    client = make_client(config, answers("x"))
    response = client.post("/api/assistant", json={**BODY, "message": message})
    assert response.status_code == 422
    assert rows(config) == []


def test_set_view_comes_back_in_camel_case(config: Config) -> None:
    turns: list[Any] = [
        [ToolCallPart("set_view", {"limit": 12, "include_below": True})],
        "Showing 12.",
    ]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = turns.pop(0)
        return ModelResponse(parts=[TextPart(turn)] if isinstance(turn, str) else turn)

    response = make_client(config, respond).post("/api/assistant", json=BODY)
    assert response.json()["actions"] == [{"kind": "set_view", "limit": 12, "includeBelow": True}]


def test_a_chat_turn_writes_an_agent_run_row_joined_to_its_tool_calls(config: Config) -> None:
    import sqlite3

    turns: list[Any] = [[ToolCallPart("set_view", {"limit": 12, "include_below": False})], "ok"]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = turns.pop(0)
        return ModelResponse(parts=[TextPart(turn)] if isinstance(turn, str) else turn)

    make_client(config, respond).post("/api/assistant", json=BODY)
    with sqlite3.connect(config.db_path) as conn:
        runs = conn.execute(
            "SELECT stage, agent_run FROM llm_calls WHERE kind = 'agent_run'"
        ).fetchall()
        tool_runs = conn.execute("SELECT agent_run FROM tool_calls").fetchall()
    assert [stage for stage, _ in runs] == ["assistant"]
    assert tool_runs == [(runs[0][1],)]


def test_a_note_is_saved_as_an_assistant_message(config: Config) -> None:
    client = make_client(config, answers("x"))
    response = client.post("/api/assistant/note", json={"text": "Found 2 new roles."})
    assert response.status_code == 201
    assert rows(config) == [("assistant", "Found 2 new roles.")]


@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
def test_an_empty_or_long_note_is_422(config: Config, text: str) -> None:
    client = make_client(config, answers("x"))
    assert client.post("/api/assistant/note", json={"text": text}).status_code == 422
    assert rows(config) == []


def test_the_old_feedback_endpoint_is_gone(config: Config) -> None:
    from career_copilot.adapters.api import review

    app = FastAPI()
    app.include_router(review.router(lambda: config))
    assert TestClient(app).post("/api/feedback", json={"note": "x"}).status_code in (404, 405)


def test_feedback_store_keeps_actions_json_oldest_first(config: Config) -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    actions = '[{"kind": "refresh", "started": true}]'
    with SqliteStore(config.db_path) as store:
        store.add_feedback("user", "hi", None, now)
        store.add_feedback("assistant", "ok", actions, now)
        entries = store.recent_feedback(20)
    assert [(e.role, e.actions) for e in entries] == [("user", None), ("assistant", actions)]


def start_run(config: Config) -> None:
    import os

    from tests.contract.test_runs_store import frozen

    now = datetime(2026, 10, 8, tzinfo=UTC)
    with SqliteStore(config.db_path) as store:
        store.begin_run("full", frozen(), now, os.getpid(), "birth")


def test_a_reply_is_not_busy_when_no_search_runs(config: Config) -> None:
    response = make_client(config, answers("Hi!")).post("/api/assistant", json=BODY)
    assert response.json()["busy"] is False


def test_a_reply_is_busy_while_a_search_runs(config: Config) -> None:
    start_run(config)
    response = make_client(config, answers("Hi!")).post("/api/assistant", json=BODY)
    assert response.json()["busy"] is True


def test_a_search_in_progress_gets_the_longer_limit(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    seen: list[float] = []
    real = asyncio.wait_for

    async def spy(awaitable: Any, timeout: float) -> Any:
        seen.append(timeout)
        return await real(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy)
    make_client(config, answers("a")).post("/api/assistant", json=BODY)
    start_run(config)
    make_client(config, answers("b")).post("/api/assistant", json=BODY)
    assert seen == [60.0, 120.0]
