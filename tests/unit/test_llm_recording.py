"""Model calls are recorded with tokens, timing and outcome, and never break the call."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from career_copilot.adapters.llm.litellm_client import LiteLLMClient
from career_copilot.ports import FailureCategory, LLMCallError, LlmCallRecord, SchemaError

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"relevance": {"type": "integer"}},
    "required": ["relevance"],
}
PROMPT = "SECRET-PROMPT-TEXT about Jane Doe"


class ListRecorder:
    def __init__(self) -> None:
        self.llm: list[LlmCallRecord] = []

    def llm_call(self, call: LlmCallRecord) -> None:
        self.llm.append(call)

    def tool_call(self, call: object) -> None:
        raise AssertionError("not used here")


class RaisingRecorder:
    def llm_call(self, call: LlmCallRecord) -> None:
        raise RuntimeError("disk full")

    def tool_call(self, call: object) -> None:
        raise RuntimeError("disk full")


def completion_with(replies: list[Any], tokens: tuple[int, int] = (100, 50)) -> Any:
    seen = 0

    def _completion(**kwargs: Any) -> Any:
        nonlocal seen
        reply = replies[min(seen, len(replies) - 1)]
        seen += 1
        if isinstance(reply, Exception):
            raise reply
        message = type("M", (), {"content": reply})()
        choice = type("C", (), {"message": message})()
        usage = type("U", (), {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]})()
        return type("R", (), {"choices": [choice], "usage": usage, "_hidden_params": {}})()

    return _completion


def ticking_clock(step: float = 2.0) -> Any:
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step  # each request reads the clock at start and end
        return state["now"]

    return clock


def client(recorder: Any, replies: list[Any]) -> LiteLLMClient:
    return LiteLLMClient(
        model="m",
        completion=completion_with(replies),
        recorder=recorder,
        stage="match",
        backend="ollama",
        profile="laptop",
        clock=ticking_clock(),
    )


def test_a_reply_with_usage_records_tokens_and_two_seconds() -> None:
    recorder = ListRecorder()
    client(recorder, ['{"relevance": 7}']).complete(PROMPT, SCHEMA)
    (row,) = recorder.llm
    assert (row.tokens_in, row.tokens_out, row.latency_s) == (100, 50, 2.0)
    assert row.tokens_out is not None
    assert row.tokens_out / row.latency_s == 25.0
    assert (row.stage, row.model, row.backend, row.profile) == ("match", "m", "ollama", "laptop")
    assert (row.kind, row.requests, row.outcome, row.retry) == ("request", 1, "ok", 0)


def test_the_response_carries_tokens_and_latency() -> None:
    response = client(ListRecorder(), ['{"relevance": 7}']).complete(PROMPT, SCHEMA)
    assert (response.tokens_in, response.tokens_out, response.latency_s) == (100, 50, 2.0)


def test_a_provider_failure_records_its_category() -> None:
    class Boom(Exception):
        status_code = 429

    recorder = ListRecorder()
    with pytest.raises(LLMCallError):
        client(recorder, [Boom("x")]).complete(PROMPT, SCHEMA)
    (row,) = recorder.llm
    assert row.outcome == FailureCategory.RATE_LIMIT.value
    assert row.tokens_in is None and row.tokens_out is None


def test_a_schema_failure_records_each_attempt_with_its_tokens() -> None:
    recorder = ListRecorder()
    with pytest.raises(SchemaError):
        client(recorder, ["not json", "still not json"]).complete(PROMPT, SCHEMA)
    assert [(r.outcome, r.retry, r.tokens_out) for r in recorder.llm] == [
        ("schema", 0, 50),
        ("schema", 1, 50),
    ]


def test_a_recorder_that_raises_does_not_fail_the_call() -> None:
    response = client(RaisingRecorder(), ['{"relevance": 7}']).complete(PROMPT, SCHEMA)
    assert response.data == {"relevance": 7}


def test_a_call_without_a_recorder_still_works() -> None:
    response = LiteLLMClient(model="m", completion=completion_with(['{"relevance": 7}'])).complete(
        PROMPT, SCHEMA
    )
    assert response.data == {"relevance": 7}


def test_no_record_field_holds_prompt_text() -> None:
    recorder = ListRecorder()
    client(recorder, ['{"relevance": 7}']).complete(PROMPT, SCHEMA)
    (row,) = recorder.llm
    for value in asdict(row).values():
        assert "SECRET" not in str(value) and "Jane" not in str(value)
