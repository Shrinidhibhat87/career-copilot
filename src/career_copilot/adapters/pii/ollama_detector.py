"""Optional local-model recall pass for PII detection."""

from __future__ import annotations

import json
from collections.abc import Sequence

import httpx

from career_copilot.ports import PII_CATEGORIES, Detection, PiiRedactionError

_PROMPT = """Find personally identifying information in the text below.
Return JSON exactly in this shape:
{"entities": [{"text": "<exact substring>", "category": "<CATEGORY>"}]}
Allowed categories: %s
Copy each value exactly. Do not paraphrase or report offsets.
If none is present, return {"entities": []}.

TEXT:
%s"""


class OllamaProtocolError(RuntimeError):
    """The local model did not return the agreed JSON shape."""


class OllamaDetector:
    """Ask a local Ollama model for values, then locate those values locally."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        base = endpoint.rstrip("/")
        self._generate_url = f"{base}/api/generate"
        self._tags_url = f"{base}/api/tags"
        self._model = model
        self._timeout = timeout_seconds
        self._client = client or httpx.Client()

    def check_ready(self) -> None:
        """Verify reachability and model availability before a scoring run starts."""
        try:
            response = self._client.get(self._tags_url, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
            models = payload["models"]
            if not isinstance(models, list):
                raise TypeError("models is not a list")
            names = {
                str(item["name"]) for item in models if isinstance(item, dict) and "name" in item
            }
        except Exception as exc:
            raise PiiRedactionError(
                f"the PII recall model is not reachable ({type(exc).__name__})"
            ) from exc
        if self._model not in names and f"{self._model}:latest" not in names:
            raise PiiRedactionError(
                f"the configured PII recall model is not pulled; run: ollama pull {self._model}"
            )

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        return [self._detect_one(text) for text in texts]

    def _detect_one(self, text: str) -> list[Detection]:
        if not text.strip():
            return []
        response = self._client.post(
            self._generate_url,
            timeout=self._timeout,
            json={
                "model": self._model,
                "prompt": _PROMPT % (", ".join(PII_CATEGORIES), text),
                "format": "json",
                "stream": False,
                "options": {"temperature": 0},
            },
        )
        response.raise_for_status()
        try:
            outer = response.json()
            payload = json.loads(outer["response"])
            entities = payload["entities"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise OllamaProtocolError(f"malformed detection reply ({type(exc).__name__})") from exc
        if not isinstance(entities, list):
            raise OllamaProtocolError("entities is not a list")
        return self._locate(text, entities)

    @staticmethod
    def _locate(text: str, entities: list[object]) -> list[Detection]:
        spans: set[Detection] = set()
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            value = entity.get("text")
            category = entity.get("category")
            if not isinstance(value, str) or not value or category not in PII_CATEGORIES:
                continue
            start = text.find(value)
            while start != -1:
                spans.add(Detection(start=start, length=len(value), category=str(category)))
                start = text.find(value, start + len(value))
        return sorted(spans, key=lambda span: (span.start, span.length, span.category))
