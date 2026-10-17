"""In-memory LLMClient. Keeps CI offline and makes agent behaviour assertable."""

from __future__ import annotations

from career_copilot.ports import LLMResponse, ResponseValidator, SchemaError, validate_schema


class FakeLLM:
    """Scripted responses matched by prompt substring, in insertion order."""

    def __init__(
        self,
        responses: dict[str, dict[str, object]] | None = None,
        default: dict[str, object] | None = None,
        model: str = "fake",
        cost_per_call_eur: float = 0.0,
    ) -> None:
        self._responses = dict(responses or {})
        self._default = default
        self.model = model
        self.cost_per_call_eur = cost_per_call_eur
        self.calls: list[str] = []

    def script(self, prompt_contains: str, response: dict[str, object]) -> None:
        self._responses[prompt_contains] = response

    def complete(
        self,
        prompt: str,
        schema: dict[str, object],
        system: str | None = None,
        validator: ResponseValidator | None = None,
    ) -> LLMResponse:
        self.calls.append(prompt)
        for needle, response in self._responses.items():
            if needle in prompt:
                return self._response(response, schema, validator)
        if self._default is None:
            raise KeyError(f"no scripted response for prompt: {prompt[:80]!r}")
        return self._response(self._default, schema, validator)

    def _response(
        self,
        response: dict[str, object],
        schema: dict[str, object],
        validator: ResponseValidator | None,
    ) -> LLMResponse:
        try:
            data = validate_schema(dict(response), schema)
            if validator is not None:
                validator(data)
        except SchemaError as exc:
            raise SchemaError(
                str(exc),
                calls=1,
                cost_eur=self.cost_per_call_eur,
                category=exc.category,
            ) from exc
        return LLMResponse(data, self.model, self.cost_per_call_eur)
