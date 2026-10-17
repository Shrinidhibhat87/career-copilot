"""HTTP for the feedback chat: one message in, one reply and its actions out.

A driving adapter only. The server keeps no chat state: the page sends the last turns and
the view it is showing, and applies the actions it gets back.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException
from pydantic_ai import Agent

from career_copilot.adapters.api.intent import default_redact
from career_copilot.adapters.observability import agent_recording
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.assistant.agent import build_assistant
from career_copilot.agents.assistant.tools import MAX_LIMIT, AssistantDeps, Refresh, ViewState
from career_copilot.agents.discovery.agent import build_model
from career_copilot.agents.recording import AgentRecording
from career_copilot.config import Config
from career_copilot.domain.intent import MAX_ENTRY_CHARS
from career_copilot.usecases.assistant import HISTORY_TURNS, Turn, converse
from career_copilot.usecases.condense_intent import Redact


def now() -> datetime:
    return datetime.now(UTC)


# What the page writes into the thread when a search it started has finished.
MAX_NOTE_CHARS = 500
# Marks the row as page-written, so the model reads it back as a fact, not as its own claim.
NOTE_ACTIONS = '[{"kind": "run_result"}]'


def default_recording(config: Config) -> AgentRecording | None:
    return agent_recording(config, config.role("assistant"), "assistant")


def default_agent(
    config: Config, recording: AgentRecording | None = None
) -> Agent[AssistantDeps, str]:
    return build_assistant(build_model(config.role("assistant")), recording)


def _cloud_consented(config: Config) -> bool:
    """A cloud assistant reads the conversation and the jobs, redacted. It runs only once
    the user has accepted the cloud notice in Settings (spec 014)."""
    with SqliteStore(config.db_path) as store:
        settings = store.model_settings()
    return bool(settings and settings.cloud_consent)


def _422(field: str, message: str) -> HTTPException:
    return HTTPException(422, detail={"errors": {field: message}})


def _history(raw: object) -> list[Turn]:
    if not isinstance(raw, list):
        return []
    turns = [
        Turn(str(t["role"]), str(t["text"]))
        for t in raw
        if isinstance(t, dict) and t.get("role") in ("user", "assistant") and "text" in t
    ]
    return turns[-HISTORY_TURNS:]


def _view(raw: object) -> ViewState:
    view = raw if isinstance(raw, dict) else {}
    limit = view.get("limit")
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else 10
    return ViewState(max(1, min(limit, MAX_LIMIT)), bool(view.get("includeBelow", False)))


def router(
    get_config: Callable[[], Config],
    agent_factory: Callable[
        [Config, AgentRecording | None], Agent[AssistantDeps, str]
    ] = default_agent,
    recording_factory: Callable[[Config], AgentRecording | None] = default_recording,
    redact_factory: Callable[[Config], Redact | None] = default_redact,
    on_refresh: Refresh | None = None,
) -> APIRouter:
    routes = APIRouter()

    @routes.post("/api/assistant")
    def assist(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        config = get_config()
        if not config.role("assistant").is_local and not _cloud_consented(config):
            raise HTTPException(
                409,
                detail={
                    "errors": {
                        "model": "The assistant needs a local model, or a cloud model "
                        "chosen in Settings"
                    }
                },
            )
        raw = body.get("message")
        message = raw.strip() if isinstance(raw, str) else ""
        if not message:
            raise _422("message", "write something first")
        if len(message) > MAX_ENTRY_CHARS:
            raise _422("message", f"keep it under {MAX_ENTRY_CHARS} characters")
        if on_refresh is None:
            raise HTTPException(500, detail={"errors": {"server": "refresh is not wired"}})
        # One recording serves the run row and its tool rows, so Settings can join them.
        recording = recording_factory(config)
        reply = converse(
            config,
            lambda: agent_factory(config, recording),
            message,
            _history(body.get("history")),
            _view(body.get("view")),
            redact=redact_factory(config),
            refresh=on_refresh,
            now=now,
            recording=recording,
        )
        return {
            "text": reply.text,
            "actions": [a.model_dump(by_alias=True, exclude_none=True) for a in reply.actions],
            "busy": reply.busy,
        }

    @routes.post("/api/assistant/note", status_code=201)
    def note(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        """The page reports how a search it started ended, so the thread keeps the answer."""
        raw = body.get("text")
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            raise _422("text", "write something first")
        if len(text) > MAX_NOTE_CHARS:
            raise _422("text", f"keep it under {MAX_NOTE_CHARS} characters")
        with SqliteStore(get_config().db_path) as store:
            store.add_feedback("assistant", text, NOTE_ACTIONS, now())
        return {"saved": True}

    return routes
