from __future__ import annotations

import json

import httpx
import pytest

from career_copilot.adapters.pii.ollama_detector import OllamaDetector, OllamaProtocolError
from career_copilot.ports import Detection, PiiDetector, PiiRedactionError


def detector(replies: list[str]) -> tuple[OllamaDetector, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"response": replies[min(len(seen) - 1, len(replies) - 1)]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaDetector("http://localhost:11434", "qwen2.5:3b", 30, client), seen


def test_it_satisfies_the_port() -> None:
    assert isinstance(detector(['{"entities": []}'])[0], PiiDetector)


def test_values_are_located_locally_and_every_occurrence_is_returned() -> None:
    found, _ = detector([json.dumps({"entities": [{"text": "Ada", "category": "PERSON"}]})])
    assert list(found.detect(["Ada and Ada"])[0]) == [
        Detection(0, 3, "PERSON"),
        Detection(8, 3, "PERSON"),
    ]


def test_absent_values_and_unknown_categories_are_discarded() -> None:
    reply = json.dumps(
        {
            "entities": [
                {"text": "Grace", "category": "PERSON"},
                {"text": "Ada", "category": "NICKNAME"},
            ]
        }
    )
    found, _ = detector([reply])
    assert list(found.detect(["Hi Ada"])[0]) == []


@pytest.mark.parametrize("reply", ["not json", '{"entities": {}}', "{}"])
def test_malformed_replies_raise(reply: str) -> None:
    found, _ = detector([reply])
    with pytest.raises(OllamaProtocolError):
        found.detect(["Hi Ada"])


def test_request_uses_configured_endpoint_model_and_json_mode() -> None:
    found, seen = detector(['{"entities": []}'])
    found.detect(["Hi Ada"])
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "http://localhost:11434/api/generate"
    assert body["model"] == "qwen2.5:3b"
    assert body["format"] == "json"
    assert body["stream"] is False


def test_blank_text_is_not_sent() -> None:
    found, seen = detector(['{"entities": []}'])
    assert list(found.detect(["   "])[0]) == []
    assert seen == []


def ready_detector(tags: object) -> OllamaDetector:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=tags))
    return OllamaDetector(
        "http://localhost:11434", "qwen2.5:3b", 30, httpx.Client(transport=transport)
    )


def test_readiness_requires_the_configured_model() -> None:
    ready_detector({"models": [{"name": "qwen2.5:3b"}]}).check_ready()
    with pytest.raises(PiiRedactionError, match="not pulled"):
        ready_detector({"models": [{"name": "llama3:8b"}]}).check_ready()


def test_readiness_fails_when_ollama_is_unreachable() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    found = OllamaDetector(
        "http://localhost:11434",
        "qwen2.5:3b",
        30,
        httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(PiiRedactionError, match="not reachable"):
        found.check_ready()
