"""`LLMClient` over any provider litellm speaks: OpenRouter, Ollama, vLLM.

The class was OpenRouter-specific in name only. A model id, a base url and an optional
key are the whole difference between a Gemini call and a call to a container on
localhost, so provider selection is `adapters/llm/backends.py` and configuration —
never a second client.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from career_copilot.domain.runs import REQUEST_TIMEOUT
from career_copilot.ports import (
    FAILURE_MESSAGES,
    FailureCategory,
    LLMCallError,
    LlmCallRecord,
    LLMResponse,
    Recorder,
    ResponseValidator,
    SchemaError,
    validate_schema,
)

log = logging.getLogger(__name__)

CompletionFn = Callable[..., Any]

_JSON_INSTRUCTION = "Reply with JSON only. No prose, no code fences."


def _unfence(content: str) -> str:
    """Strip a Markdown code fence. Gemini adds one whatever the prompt says."""
    text = content.strip()
    if not text.startswith("```"):
        return text
    body = text[3:]
    newline = body.find("\n")
    if newline == -1:  # a fence with no body at all
        return ""
    body = body[newline + 1 :]
    closing = body.rfind("```")
    return (body[:closing] if closing != -1 else body).strip()


# Where litellm (1.7x+) puts the cost OpenRouter reported in the response body.
_PROVIDER_COST_HEADER = "llm_provider-x-litellm-response-cost"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _cached_tokens(usage: object) -> int:
    """Prompt tokens the provider served from its prefix cache, 0 when it does not say.

    OpenAI-shaped usage nests the count under `prompt_tokens_details`; some providers
    report it flat as `cached_tokens`.
    """
    details = getattr(usage, "prompt_tokens_details", None)
    for holder in (details, usage):
        value = getattr(holder, "cached_tokens", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def reply_cost(raw: object) -> float:
    """What a reply cost, as the provider billed it when it says.

    OpenRouter returns `usage.cost`, the amount actually charged. litellm's own price map
    reports 0 for a model it does not know, which would let a run pass its budget, so the
    map is only the last resort.
    """
    usage = getattr(raw, "usage", None)
    cost = _number(getattr(usage, "cost", None))
    if cost is None and isinstance(usage, dict):
        cost = _number(usage.get("cost"))
    if cost is not None:
        return cost
    hidden = getattr(raw, "_hidden_params", {}) or {}
    headers = hidden.get("additional_headers") or {}
    cost = _number(headers.get(_PROVIDER_COST_HEADER)) if isinstance(headers, dict) else None
    if cost is not None:
        return cost
    return float(hidden.get("response_cost") or 0.0)


class LiteLLMClient:
    """One instance per model. Temperature 0, one retry that feeds the error back.

    A local provider reports no cost, so `cost_eur` is 0.0 and the meter records a call
    that spent nothing. That is the truth, not a missing case.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        completion: CompletionFn | None = None,
        recorder: Recorder | None = None,
        stage: str = "",
        backend: str = "",
        profile: str | None = None,
        run_id: int | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.model = model
        self.base_url = base_url or os.environ.get("OPENROUTER_API_BASE")
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._completion = completion
        self._recorder = recorder
        self._stage = stage
        self._backend = backend
        self._profile = profile
        self._run_id = run_id
        self._clock = clock

    def _record(
        self,
        outcome: str,
        latency: float,
        tokens: tuple[int | None, int | None, int] = (None, None, 0),
        retry: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Hand one row to the recorder. A recorder failure is logged and dropped."""
        if self._recorder is None:
            return
        try:
            self._recorder.llm_call(
                LlmCallRecord(
                    stage=self._stage,
                    model=self.model,
                    backend=self._backend,
                    profile=self._profile,
                    run_id=self._run_id,
                    kind="request",
                    latency_s=latency,
                    outcome=outcome,
                    tokens_in=tokens[0],
                    tokens_out=tokens[1],
                    cached_tokens=tokens[2],
                    retry=retry,
                    cost_eur=cost,
                )
            )
        except Exception as exc:
            log.warning("recording a model call failed: %s", type(exc).__name__)

    def _call(
        self, messages: list[dict[str, str]], attempt: int = 0
    ) -> tuple[str, float, tuple[int | None, int | None, int], float]:
        completion = self._completion
        if completion is None:
            import litellm  # imported lazily so tests never need the package configured

            completion = litellm.completion
        started = self._clock()
        try:
            raw = completion(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                api_base=self.base_url,
                api_key=self.api_key,
                timeout=REQUEST_TIMEOUT,
                num_retries=0,
            )
        except Exception as exc:
            # Provider exceptions can echo their request body. At this layer that body is
            # redacted, but placeholders are reversal-map values and §11 forbids putting
            # those in an exception too. Preserve the failure class, never its text.
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            category = {402: FailureCategory.CREDIT, 429: FailureCategory.RATE_LIMIT}.get(
                status if isinstance(status, int) else 0, FailureCategory.TRANSPORT
            )
            log.debug("provider failure: %s status=%s", type(exc).__name__, status)
            self._record(category.value, self._clock() - started, retry=attempt)
            raise LLMCallError(
                f"{FAILURE_MESSAGES[category]} ({type(exc).__name__})",
                category=category,
                calls=1,
            ) from None
        latency = self._clock() - started
        usage = getattr(raw, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", None)
        tokens_out = getattr(usage, "completion_tokens", None)
        tokens = (
            tokens_in if isinstance(tokens_in, int) else None,
            tokens_out if isinstance(tokens_out, int) else None,
            _cached_tokens(usage),
        )
        cost = reply_cost(raw)
        choices = getattr(raw, "choices", None)
        choice = choices[0] if choices else None
        content = getattr(getattr(choice, "message", None), "content", None)
        if (
            getattr(choice, "finish_reason", None) in {"error", "content_filter"}
            or not isinstance(content, str)
            or not content.strip()
        ):
            self._record(FailureCategory.PROVIDER.value, latency, tokens, attempt, cost)
            raise LLMCallError(
                FAILURE_MESSAGES[FailureCategory.PROVIDER],
                category=FailureCategory.PROVIDER,
                calls=1,
                cost_eur=cost,
            )
        return content, cost, tokens, latency

    def complete(
        self,
        prompt: str,
        schema: dict[str, object],
        system: str | None = None,
        validator: ResponseValidator | None = None,
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append(
            {
                "role": "user",
                "content": f"{prompt}\n\n{_JSON_INSTRUCTION}\nSchema: {json.dumps(schema)}",
            }
        )

        total_cost = 0.0
        last_error = ""
        for attempt in (1, 2):
            try:
                content, cost, tokens, latency = self._call(messages, attempt - 1)
            except LLMCallError as exc:
                raise LLMCallError(
                    str(exc),
                    category=exc.category,
                    calls=attempt,
                    cost_eur=total_cost + exc.cost_eur,
                ) from exc
            total_cost += cost
            try:
                data = json.loads(_unfence(content))
                validated = validate_schema(data, schema)
                if validator is not None:
                    validator(validated)
                self._record("ok", latency, tokens, attempt - 1, cost)
                return LLMResponse(
                    validated,
                    self.model,
                    total_cost,
                    calls=attempt,
                    tokens_in=tokens[0],
                    tokens_out=tokens[1],
                    latency_s=latency,
                    cached_tokens=tokens[2],
                )
            except LLMCallError as exc:
                if exc.category != FailureCategory.SCHEMA:
                    self._record(exc.category.value, latency, tokens, attempt - 1, cost)
                    raise LLMCallError(
                        FAILURE_MESSAGES[exc.category],
                        category=exc.category,
                        calls=attempt,
                        cost_eur=total_cost,
                    ) from None
                last_error = str(exc)
                self._record(FailureCategory.SCHEMA.value, latency, tokens, attempt - 1, cost)
            except json.JSONDecodeError:
                last_error = "not valid JSON"
                self._record(FailureCategory.SCHEMA.value, latency, tokens, attempt - 1, cost)
            log.warning("schema failure on attempt %d: %s", attempt, last_error)
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": f"That reply was rejected: {last_error}. {_JSON_INSTRUCTION}",
                }
            )
        raise SchemaError(last_error, calls=2, cost_eur=total_cost)
