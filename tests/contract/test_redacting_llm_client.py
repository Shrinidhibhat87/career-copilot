"""`RedactingLLMClient` must satisfy the same `LLMClient` port contract as the clients it
wraps (design doc §14, "Port contract" row). `tests/contract/test_llm_client.py` stays
untouched; this file re-asserts its contract through the decorator."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.llm.redacting_client import RedactingLLMClient
from career_copilot.adapters.pii.redactor import Redactor
from career_copilot.ports import (
    Detection,
    LLMClient,
    LLMResponse,
    ResponseValidator,
    SchemaError,
)

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"relevance": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["relevance", "reason"],
}

ALL_ENTITIES = ("PERSON", "EMAIL", "PHONE", "ADDRESS", "URL", "IBAN", "CREDIT_CARD", "IP")


class RecordingLLM:
    """Records the exact `(prompt, system)` pair the inner client is handed."""

    model = "recording"

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None]] = []

    def complete(
        self,
        prompt: str,
        schema: dict[str, object],
        system: str | None = None,
        validator: ResponseValidator | None = None,
    ) -> LLMResponse:
        self.seen.append((prompt, system))
        return LLMResponse({"relevance": 1, "reason": "x"}, self.model, 0.0)


class NullDetector:
    """Finds nothing. Keeps these tests about the port contract, not about detection."""

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        return [[] for _ in texts]


@pytest.fixture
def client() -> RedactingLLMClient:
    inner = FakeLLM(default={"relevance": 50, "reason": "default"})
    return RedactingLLMClient(inner, Redactor(NullDetector(), ALL_ENTITIES))


def test_the_decorator_satisfies_the_port(client: RedactingLLMClient) -> None:
    assert isinstance(client, LLMClient)


def test_complete_returns_data_model_and_cost(client: RedactingLLMClient) -> None:
    response = client.complete("score this job", SCHEMA)
    assert response.data == {"relevance": 50, "reason": "default"}
    assert response.model == "fake"
    assert response.cost_eur >= 0.0


def test_scripted_response_matches_on_prompt_substring() -> None:
    inner = FakeLLM(default={"relevance": 50, "reason": "default"})
    inner.script("Acme", {"relevance": 91, "reason": "strong overlap"})
    client = RedactingLLMClient(inner, Redactor(NullDetector(), ALL_ENTITIES))
    assert client.complete("role at Acme GmbH", SCHEMA).data["relevance"] == 91
    assert client.complete("role at Other", SCHEMA).data["relevance"] == 50


def test_calls_reach_the_inner_client() -> None:
    inner = FakeLLM(default={"relevance": 50, "reason": "default"})
    client = RedactingLLMClient(inner, Redactor(NullDetector(), ALL_ENTITIES))
    client.complete("first", SCHEMA)
    client.complete("second", SCHEMA)
    assert len(inner.calls) == 2


def test_a_schema_error_propagates_through_the_decorator() -> None:
    inner = FakeLLM(default={"reason": "no score"})
    client = RedactingLLMClient(inner, Redactor(NullDetector(), ALL_ENTITIES))
    with pytest.raises(SchemaError, match="relevance"):
        client.complete("prompt", SCHEMA)


def test_the_model_name_is_not_hidden_by_the_wrapper(client: RedactingLLMClient) -> None:
    """`ScoreRun` selects jobs by `MatchAgent.model`; the decorator must be transparent."""
    assert client.model == "fake"


def test_a_system_prompt_is_forwarded() -> None:
    inner = RecordingLLM()
    client = RedactingLLMClient(inner, Redactor(NullDetector(), ALL_ENTITIES))
    client.complete("prompt", SCHEMA, system="you are a scorer")
    assert inner.seen == [("prompt", "you are a scorer")]


def test_an_absent_system_prompt_stays_none() -> None:
    """`redact([prompt, ""])` must not turn a missing system prompt into an empty string."""
    inner = RecordingLLM()
    client = RedactingLLMClient(inner, Redactor(NullDetector(), ALL_ENTITIES))
    client.complete("prompt", SCHEMA)
    assert inner.seen == [("prompt", None)]
