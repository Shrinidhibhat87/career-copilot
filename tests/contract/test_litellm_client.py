"""Contract every LLMClient adapter must satisfy, including the fake."""

from __future__ import annotations

from typing import Any

import pytest

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.llm.litellm_client import LiteLLMClient
from career_copilot.domain.runs import REQUEST_TIMEOUT
from career_copilot.ports import FailureCategory, LLMCallError, LLMClient, SchemaError

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"relevance": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["relevance", "reason"],
}


@pytest.fixture
def client() -> FakeLLM:
    return FakeLLM(default={"relevance": 50, "reason": "default"})


def test_fake_satisfies_the_port(client: FakeLLM) -> None:
    assert isinstance(client, LLMClient)


def test_complete_returns_data_model_and_cost(client: FakeLLM) -> None:
    response = client.complete("score this job", SCHEMA)
    assert response.data == {"relevance": 50, "reason": "default"}
    assert response.model == "fake"
    assert response.cost_eur >= 0.0


def test_scripted_response_matches_on_prompt_substring(client: FakeLLM) -> None:
    client.script("Acme", {"relevance": 91, "reason": "strong overlap"})
    assert client.complete("role at Acme GmbH", SCHEMA).data["relevance"] == 91
    assert client.complete("role at Other", SCHEMA).data["relevance"] == 50


def test_calls_are_recorded(client: FakeLLM) -> None:
    client.complete("first", SCHEMA)
    client.complete("second", SCHEMA)
    assert len(client.calls) == 2


def test_an_unscripted_prompt_without_a_default_raises() -> None:
    with pytest.raises(KeyError, match="no scripted response"):
        FakeLLM().complete("anything", SCHEMA)


def stub_completion(replies: list[str], cost: float = 0.001) -> Any:
    calls: list[dict[str, Any]] = []

    def _completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        content = replies[min(len(calls) - 1, len(replies) - 1)]
        message = type("M", (), {"content": content})()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice], "_hidden_params": {"response_cost": cost}})()

    _completion.calls = calls  # type: ignore[attr-defined]
    return _completion


def test_openrouter_client_satisfies_the_port() -> None:
    client = LiteLLMClient(
        model="m", completion=stub_completion(['{"relevance": 1, "reason": "x"}'])
    )
    assert isinstance(client, LLMClient)


def test_credentials_come_from_the_openrouter_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_API_BASE", "https://openrouter.example/api/v1")
    completion = stub_completion(['{"relevance": 1, "reason": "x"}'])
    LiteLLMClient(model="openrouter/google/gemini-2.5-flash", completion=completion).complete(
        "prompt", SCHEMA
    )
    call = completion.calls[0]
    assert call["api_key"] == "sk-or-test"
    assert call["api_base"] == "https://openrouter.example/api/v1"
    assert call["model"] == "openrouter/google/gemini-2.5-flash"


def test_a_fenced_reply_is_unwrapped() -> None:
    """Gemini answers with a ```json fence despite the instruction not to."""
    fenced = '```json\n{"relevance": 4, "reason": "stack mismatch"}\n```'
    completion = stub_completion([fenced])
    response = LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert response.data == {"relevance": 4, "reason": "stack mismatch"}
    assert len(completion.calls) == 1, "unwrapping must not cost a retry"


def test_a_bare_fence_without_a_language_tag_is_unwrapped() -> None:
    completion = stub_completion(['```\n{"relevance": 7, "reason": "ok"}\n```'])
    response = LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert response.data["relevance"] == 7


def test_valid_json_is_returned_with_model_and_cost() -> None:
    client = LiteLLMClient(
        model="m", completion=stub_completion(['{"relevance": 88, "reason": "fits"}'], cost=0.002)
    )
    response = client.complete("prompt", SCHEMA)
    assert response.data == {"relevance": 88, "reason": "fits"}
    assert response.model == "m"
    assert response.cost_eur == pytest.approx(0.002)


def test_temperature_is_zero_by_default() -> None:
    completion = stub_completion(['{"relevance": 1, "reason": "x"}'])
    LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert completion.calls[0]["temperature"] == 0.0


def test_openrouter_request_has_an_explicit_output_token_cap() -> None:
    completion = stub_completion(['{"relevance": 1, "reason": "x"}'])
    LiteLLMClient(model="m", max_tokens=321, completion=completion).complete("prompt", SCHEMA)
    assert completion.calls[0]["max_tokens"] == 321


def test_invalid_json_retries_once_then_succeeds() -> None:
    completion = stub_completion(["not json at all", '{"relevance": 5, "reason": "ok"}'])
    response = LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert response.data["relevance"] == 5
    assert len(completion.calls) == 2


def test_wrong_field_type_retries_instead_of_being_coerced() -> None:
    completion = stub_completion(
        ['{"relevance": "high", "reason": "x"}', '{"relevance": 75, "reason": "fixed"}']
    )
    response = LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert response.data["relevance"] == 75
    assert len(completion.calls) == 2


def test_domain_validation_uses_the_same_retry_path() -> None:
    completion = stub_completion(
        ['{"relevance": 10, "reason": "unsupported"}', '{"relevance": 75, "reason": "fixed"}']
    )

    def require_high_score(data: dict[str, object]) -> None:
        if data["relevance"] == 10:
            raise SchemaError("score lacks supporting evidence")

    response = LiteLLMClient(model="m", completion=completion).complete(
        "prompt", SCHEMA, validator=require_high_score
    )
    assert response.data["relevance"] == 75
    assert len(completion.calls) == 2


def test_the_retry_feeds_the_error_back() -> None:
    completion = stub_completion(["not json at all", '{"relevance": 5, "reason": "ok"}'])
    LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    retry_prompt = completion.calls[1]["messages"][-1]["content"]
    assert "not valid JSON" in retry_prompt or "required" in retry_prompt


def test_a_missing_required_field_is_a_schema_error() -> None:
    completion = stub_completion(['{"reason": "no score"}', '{"reason": "still none"}'])
    with pytest.raises(SchemaError, match="relevance"):
        LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)


def test_two_failures_raise_rather_than_retrying_forever() -> None:
    completion = stub_completion(["nope", "still nope"])
    with pytest.raises(SchemaError):
        LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert len(completion.calls) == 2


def test_failed_attempts_preserve_calls_and_cost() -> None:
    completion = stub_completion(["nope", "still nope"], cost=0.003)
    with pytest.raises(SchemaError) as caught:
        LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert caught.value.calls == 2
    assert caught.value.cost_eur == pytest.approx(0.006)


def test_provider_exception_text_is_not_copied_to_the_public_error() -> None:
    def completion(**kwargs: Any) -> Any:
        raise RuntimeError("request contained <|PERSON_1|>")

    with pytest.raises(Exception) as caught:
        LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert "<|PERSON_1|>" not in str(caught.value)
    assert "RuntimeError" in str(caught.value)


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (402, FailureCategory.CREDIT),
        (429, FailureCategory.RATE_LIMIT),
        (503, FailureCategory.TRANSPORT),
    ],
)
def test_provider_failures_are_classified_and_never_retried(
    status: int, category: FailureCategory
) -> None:
    calls: list[dict[str, Any]] = []

    def completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        error = RuntimeError("private nested provider payload")
        error.status_code = status  # type: ignore[attr-defined]
        raise error

    with pytest.raises(LLMCallError) as caught:
        LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert caught.value.category is category
    assert caught.value.calls == 1
    assert len(calls) == 1
    assert "private nested provider payload" not in str(caught.value)


def test_empty_content_is_a_one_call_provider_failure() -> None:
    completion = stub_completion([""])
    with pytest.raises(LLMCallError) as caught:
        LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert caught.value.category is FailureCategory.PROVIDER
    assert caught.value.calls == 1
    assert caught.value.cost_eur == pytest.approx(0.001)
    assert len(completion.calls) == 1


def test_request_timeout_and_disabled_transport_retries_reach_litellm() -> None:
    completion = stub_completion(['{"relevance": 1, "reason": "x"}'])
    LiteLLMClient(model="m", completion=completion).complete("prompt", SCHEMA)
    assert completion.calls[0]["timeout"] == REQUEST_TIMEOUT
    assert completion.calls[0]["num_retries"] == 0


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    cost = 0.0042


class _Message:
    content = '{"relevance": 70, "reason": "fits"}'


class _Choice:
    message = _Message()
    finish_reason = "stop"


class _Reply:
    def __init__(self) -> None:
        self.usage: object = _Usage()
        self.choices = [_Choice()]
        # litellm's own price map does not know the model and says 0.
        self._hidden_params: dict[str, object] = {"response_cost": 0.0}


def test_the_meter_is_charged_openrouters_reported_cost() -> None:
    client = LiteLLMClient(model="openrouter/new/model", completion=lambda **_: _Reply())
    assert client.complete("prompt", SCHEMA).cost_eur == pytest.approx(0.0042)


def test_the_cost_litellm_lifted_from_the_body_is_used_when_usage_lacks_it() -> None:
    reply = _Reply()
    reply.usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
    reply._hidden_params = {
        "response_cost": 0.0,
        "additional_headers": {"llm_provider-x-litellm-response-cost": 0.0031},
    }
    client = LiteLLMClient(model="openrouter/new/model", completion=lambda **_: reply)
    assert client.complete("prompt", SCHEMA).cost_eur == pytest.approx(0.0031)
