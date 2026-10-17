"""Provider prefix caching: the prompt keeps its static part first, the redactor keeps that
part byte-identical across postings, and the cache hit is recorded."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from career_copilot.adapters.llm.litellm_client import LiteLLMClient
from career_copilot.adapters.llm.redacting_client import RedactingLLMClient
from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.agents.match import SYSTEM, MatchAgent
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.profile import Profile
from career_copilot.ports import LlmCallRecord, LLMResponse, ResponseValidator
from tests.conftest import make_raw
from tests.unit.test_llm_recording import SCHEMA, ListRecorder, ticking_clock
from tests.unit.test_redacting_client import redactor_for

POSTING_MARK = "POSTING\n"
PROFILE = Profile(
    preferences="Forward-deployed AI roles in Berlin.",
    resume="Ada Lovelace built a LangGraph POC on Bedrock.\nShipped RAG to production.",
)


def _agent() -> MatchAgent:
    return MatchAgent(client=None, profile=PROFILE)  # type: ignore[arg-type]


def test_nothing_posting_specific_comes_before_the_resume() -> None:
    first = to_posting(make_raw(title="Alpha Role", company="Alpha Inc", description="aaa"))
    second = to_posting(make_raw(title="Beta Role", company="Beta Inc", description="bbb"))
    a = _agent().build_prompt(first)
    b = _agent().build_prompt(second)
    assert a.index(POSTING_MARK) == b.index(POSTING_MARK)
    prefix = a[: a.index(POSTING_MARK)]
    assert prefix == b[: b.index(POSTING_MARK)]
    assert prefix.index("PREFERENCES") < prefix.index("RESUME")
    assert "Alpha" not in prefix


def test_the_redacted_prefix_is_identical_across_postings_with_different_pii() -> None:
    seen: list[tuple[str, str | None]] = []

    class Capture:
        model = "cap"

        def complete(
            self,
            prompt: str,
            schema: dict[str, object],
            system: str | None = None,
            validator: ResponseValidator | None = None,
        ) -> LLMResponse:
            seen.append((prompt, system))
            raise RuntimeError("stop after capture")

    values = {
        "Ada Lovelace": "PERSON",
        "Grace Hopper": "PERSON",
        "Alan Turing": "PERSON",
        "ada@lovelace.io": "EMAIL",
    }
    client = RedactingLLMClient(Capture(), redactor_for(values))
    agent = MatchAgent(client, PROFILE)
    postings = (
        "Contact Grace Hopper at ada@lovelace.io",
        "Ask Alan Turing, then Grace Hopper",
    )
    for description in postings:
        with contextlib.suppress(RuntimeError):
            agent.evaluate(to_posting(make_raw(description=description)))
    (p1, s1), (p2, s2) = seen
    assert s1 == s2
    assert p1[: p1.index(POSTING_MARK)] == p2[: p2.index(POSTING_MARK)]
    assert "<|PERSON_1|>" in p1[: p1.index(POSTING_MARK)]
    assert p1 != p2


def _completion(usage: dict[str, Any]) -> Any:
    def run(**kwargs: Any) -> Any:
        message = type("M", (), {"content": '{"relevance": 1}'})()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice], "usage": _usage(usage), "_hidden_params": {}})()

    return run


def _usage(fields: dict[str, Any]) -> Any:
    values = dict(fields)
    details = values.pop("prompt_tokens_details", None)
    if isinstance(details, dict):
        values["prompt_tokens_details"] = type("D", (), details)()
    return type("U", (), values)()


def _record(usage: dict[str, Any]) -> LlmCallRecord:
    recorder = ListRecorder()
    client = LiteLLMClient(
        model="m",
        completion=_completion(usage),
        recorder=recorder,
        stage="match",
        backend="openrouter",
        clock=ticking_clock(),
    )
    response = client.complete("prompt", SCHEMA, system=SYSTEM)
    assert response.cached_tokens == recorder.llm[0].cached_tokens
    return recorder.llm[0]


def test_nested_cached_tokens_are_recorded() -> None:
    call = _record(
        {
            "prompt_tokens": 1500,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 1280},
        }
    )
    assert call.cached_tokens == 1280


def test_flat_cached_tokens_are_recorded() -> None:
    assert (
        _record({"prompt_tokens": 900, "completion_tokens": 10, "cached_tokens": 512}).cached_tokens
        == 512
    )


def test_a_provider_that_reports_nothing_reads_zero() -> None:
    assert _record({"prompt_tokens": 100, "completion_tokens": 10}).cached_tokens == 0


def test_cached_tokens_survive_the_store(tmp_path: Path) -> None:
    store = ObservabilityStore(tmp_path / "jobs.db")
    store.record_llm(
        LlmCallRecord(
            stage="match",
            model="m",
            backend="openrouter",
            kind="request",
            latency_s=1.0,
            outcome="ok",
            tokens_in=1500,
            tokens_out=40,
            cached_tokens=1280,
        ),
        datetime(2026, 10, 15, tzinfo=UTC),
    )
    with sqlite3.connect(tmp_path / "jobs.db") as conn:
        assert conn.execute("SELECT cached_tokens FROM llm_calls").fetchall() == [(1280,)]
