"""Agent runs record one agent_run row and one tool_calls row per tool call."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from career_copilot.agents.curator.agent import build_curator
from career_copilot.agents.curator.tools import CuratorDeps
from career_copilot.agents.recording import AgentRecording
from career_copilot.ports import LlmCallRecord, ToolCallRecord


class ListRecorder:
    def __init__(self) -> None:
        self.llm: list[LlmCallRecord] = []
        self.tools: list[ToolCallRecord] = []

    def llm_call(self, call: LlmCallRecord) -> None:
        self.llm.append(call)

    def tool_call(self, call: ToolCallRecord) -> None:
        self.tools.append(call)


class RaisingRecorder:
    def llm_call(self, call: LlmCallRecord) -> None:
        raise RuntimeError("disk full")

    def tool_call(self, call: ToolCallRecord) -> None:
        raise RuntimeError("disk full")


def scripted(*turns: list[ToolCallPart] | str) -> FunctionModel:
    remaining = list(turns)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = remaining.pop(0) if remaining else "done"
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=list(turn))

    return FunctionModel(respond)


def deps() -> CuratorDeps:
    return CuratorDeps(pool=[], vectors={}, embed=lambda text: [0.0], limit=3)


def recording(recorder: Any, clock: Any = None) -> AgentRecording:
    kwargs: dict[str, Any] = {} if clock is None else {"clock": clock}
    return AgentRecording(
        recorder, agent="curator", model="m", backend="ollama", profile="laptop", **kwargs
    )


def run(rec: AgentRecording, model: FunctionModel) -> Any:
    rec.start()
    agent = build_curator(model, recording=rec)
    return asyncio.run(agent.run("go", deps=deps()))


def test_one_agent_run_row_and_one_row_per_tool_call_including_a_refusal() -> None:
    recorder = ListRecorder()
    rec = recording(recorder)
    model = scripted(
        [ToolCallPart("search_pool", {"query": "data engineer"})],
        [ToolCallPart("shortlist", {"job_id": 999, "reason": "x"})],
        "finished",
    )
    result = run(rec, model)
    rec.finish(result)

    assert [(t.agent, t.tool, t.outcome) for t in recorder.tools] == [
        ("curator", "search_pool", "ok"),
        ("curator", "shortlist", "refused"),
    ]
    (row,) = recorder.llm
    assert (row.kind, row.stage, row.model, row.backend, row.profile) == (
        "agent_run",
        "curator",
        "m",
        "ollama",
        "laptop",
    )
    assert row.requests == 3
    assert row.outcome == "ok"
    assert row.tokens_in is not None and row.tokens_out is not None


def test_agent_time_is_the_run_time_minus_the_time_in_tools() -> None:
    recorder = ListRecorder()
    ticks = iter([0.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0])
    rec = recording(recorder, clock=lambda: next(ticks))
    rec.start()  # t=0
    wrapped = rec.timed_tool("search_pool", lambda: "ok")
    wrapped()  # reads 10.0 then 11.0: 1 s in the tool
    rec.finish(None, outcome="timeout")  # reads 12.0: 12 s total, 11 s outside tools
    (row,) = recorder.llm
    assert row.latency_s == 11.0
    assert row.outcome == "timeout"
    assert row.tokens_in is None


def test_tool_rows_and_the_run_row_share_one_run_id_that_changes_per_start() -> None:
    recorder = ListRecorder()
    rec = recording(recorder)
    run(rec, scripted([ToolCallPart("search_pool", {"query": "x"})], "done"))
    rec.finish(None)
    first = recorder.llm[0].agent_run
    assert first is not None
    assert {t.agent_run for t in recorder.tools} == {first}
    rec.start()
    rec.finish(None)
    assert recorder.llm[1].agent_run not in (None, first)


def test_a_tool_that_raises_is_recorded_as_an_error_and_still_raises() -> None:
    recorder = ListRecorder()
    rec = recording(recorder)

    def boom() -> str:
        raise ValueError("secret detail")

    with pytest.raises(ValueError):
        rec.timed_tool("shortlist", boom)()
    assert [(t.tool, t.outcome) for t in recorder.tools] == [("shortlist", "error")]


def test_a_failed_search_string_counts_as_an_error() -> None:
    recorder = ListRecorder()
    rec = recording(recorder)
    rec.timed_tool("web_search", lambda: "search failed: boom")()
    assert recorder.tools[0].outcome == "error"


def test_a_recorder_that_raises_does_not_fail_the_run() -> None:
    rec = recording(RaisingRecorder())
    model = scripted([ToolCallPart("search_pool", {"query": "x"})], "finished")
    result = run(rec, model)
    rec.finish(result)
    assert result.output == "finished"


def test_no_recording_means_the_agent_works_unchanged() -> None:
    agent = build_curator(scripted([ToolCallPart("search_pool", {"query": "x"})], "finished"))
    assert asyncio.run(agent.run("go", deps=deps())).output == "finished"


def test_tool_arguments_are_not_stored() -> None:
    recorder = ListRecorder()
    rec = recording(recorder)
    run(rec, scripted([ToolCallPart("search_pool", {"query": "SECRET-QUERY"})], "done"))
    assert "SECRET" not in repr(recorder.tools)
