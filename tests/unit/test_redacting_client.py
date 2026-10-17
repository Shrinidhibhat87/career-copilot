"""The redaction boundary itself (design doc §5, §8, §9, §11, §14 "Decorator"/"Logging").

Nothing here touches the network, Ollama, or spaCy: detection is a stub, and the provider
is either `FakeLLM` or `LiteLLMClient` with an injected `completion` callable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, cast

import pytest

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.llm.litellm_client import LiteLLMClient
from career_copilot.adapters.llm.redacting_client import RedactingLLMClient
from career_copilot.adapters.pii.factory import build_redactor
from career_copilot.adapters.pii.person_policy import PersonPolicy
from career_copilot.adapters.pii.redactor import Redactor
from career_copilot.agents.match import MatchAgent
from career_copilot.config import Config, PiiSpec
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.profile import Profile
from career_copilot.ports import (
    Detection,
    LLMResponse,
    PiiRedactionError,
    ResponseValidator,
    SchemaError,
)
from tests.conftest import make_raw
from tests.contract.test_litellm_client import stub_completion
from tests.match_replies import reply_with_total

ALL_ENTITIES = ("PERSON", "EMAIL", "PHONE", "ADDRESS", "URL", "IBAN", "CREDIT_CARD", "IP")

NAME = "Ada Lovelace"
EMAIL = "ada@lovelace.io"

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"relevance": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["relevance", "reason"],
}


class SubstringDetector:
    """Reports every occurrence of the given values, per text. Deterministic and offline —
    the real Presidio adapter has its own unit tests."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        results: list[Sequence[Detection]] = []
        for text in texts:
            found: list[Detection] = []
            for value, category in self._values.items():
                for match in re.finditer(re.escape(value), text):
                    found.append(Detection(match.start(), len(value), category))
            results.append(found)
        return results


class FailingDetector:
    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        raise PiiRedactionError("detector unavailable")


class NeverCalledLLM:
    model = "never"

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        prompt: str,
        schema: dict[str, object],
        system: str | None = None,
        validator: ResponseValidator | None = None,
    ) -> LLMResponse:
        self.calls += 1
        raise AssertionError("the inner client must not be reached when redaction fails")


def redactor_for(values: dict[str, str]) -> Redactor:
    return Redactor(SubstringDetector(values), ALL_ENTITIES)


# --- the inner client never sees a mapping value -----------------------------------


def test_the_inner_client_receives_no_mapping_value() -> None:
    inner = FakeLLM(default={"relevance": 1, "reason": "ok"})
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON", EMAIL: "EMAIL"}))
    client.complete(f"RESUME\n{NAME} <{EMAIL}> builds pipelines", SCHEMA)
    sent = inner.calls[0]
    assert NAME not in sent
    assert EMAIL not in sent
    assert "<|PERSON_1|>" in sent
    assert "<|EMAIL_1|>" in sent


def test_the_system_prompt_is_redacted_with_the_same_map() -> None:
    seen: list[tuple[str, str | None]] = []

    class Recorder:
        model = "rec"

        def complete(
            self,
            prompt: str,
            schema: dict[str, object],
            system: str | None = None,
            validator: ResponseValidator | None = None,
        ) -> LLMResponse:
            seen.append((prompt, system))
            return LLMResponse({"relevance": 1, "reason": "ok"}, "rec", 0.0)

    client = RedactingLLMClient(Recorder(), redactor_for({NAME: "PERSON"}))
    client.complete(f"prompt about {NAME}", SCHEMA, system=f"system about {NAME}")
    prompt, system = seen[0]
    # One map for both texts: the same value gets the same token in each.
    assert prompt == "prompt about <|PERSON_1|>"
    assert system == "system about <|PERSON_1|>"


def test_the_response_is_restored_for_the_caller() -> None:
    inner = FakeLLM(default={"relevance": 1, "reason": "hire <|PERSON_1|>"})
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON"}))
    response = client.complete(f"about {NAME}", SCHEMA)
    assert response.data["reason"] == f"hire {NAME}"


# --- the caller's validator sees restored data (§8) ---------------------------------


def test_match_agents_evidence_check_passes_through_the_gateway() -> None:
    """The model reads a redacted, line-numbered resume and cites an ID. The agent resolves
    that ID against the *original* resume, so the stored evidence carries the real name."""
    profile = Profile(
        preferences="Forward-deployed AI roles in Berlin.",
        resume=f"{NAME} built a LangGraph POC on Bedrock.",
    )
    inner = FakeLLM(
        # The model cites the ID of the line it was shown.
        default=reply_with_total(82, evidence_refs=["L1"])
    )
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON"}))
    result = MatchAgent(client, profile).evaluate(to_posting(make_raw()))
    assert result.score == 82
    assert result.evidence_refs == (f"{NAME} built a LangGraph POC on Bedrock.",)
    assert NAME not in inner.calls[0]


def test_an_invented_quote_is_still_rejected_through_the_gateway() -> None:
    """Restoration must not weaken the evidence check into a rubber stamp."""
    profile = Profile(preferences="prefs", resume=f"{NAME} built a LangGraph POC.")
    inner = FakeLLM(default=reply_with_total(90, reasons=["invented"], evidence_refs=["L99"]))
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON"}))
    with pytest.raises(SchemaError, match="line ID"):
        MatchAgent(client, profile).evaluate(to_posting(make_raw()))


# --- both retry attempts carry redacted text ---------------------------------------


def test_both_retry_attempts_carry_redacted_text() -> None:
    """The retry loop lives inside `LiteLLMClient.complete`, so it is inside the
    decorator. No second redaction pass, and no raw text on the second attempt."""
    completion = stub_completion(["not json at all", '{"relevance": 5, "reason": "ok"}'])
    inner = LiteLLMClient(model="m", completion=completion)
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON", EMAIL: "EMAIL"}))
    client.complete(f"RESUME\n{NAME} <{EMAIL}>", SCHEMA, system=f"score {NAME}")

    assert len(completion.calls) == 2
    for call in completion.calls:
        payload = str(call["messages"])
        assert NAME not in payload
        assert EMAIL not in payload
        assert "<|PERSON_1|>" in payload


# --- fail closed before the call ----------------------------------------------------


def test_a_detector_failure_blocks_the_call() -> None:
    inner = NeverCalledLLM()
    client = RedactingLLMClient(inner, Redactor(FailingDetector(), ALL_ENTITIES))
    with pytest.raises(PiiRedactionError):
        client.complete("anything", SCHEMA)
    assert inner.calls == 0


def test_a_non_str_prompt_is_rejected() -> None:
    inner = NeverCalledLLM()
    client = RedactingLLMClient(inner, redactor_for({}))
    with pytest.raises(PiiRedactionError, match="prompt"):
        client.complete(cast(str, {"parts": ["image"]}), SCHEMA)
    assert inner.calls == 0


def test_a_non_str_system_is_rejected() -> None:
    inner = NeverCalledLLM()
    client = RedactingLLMClient(inner, redactor_for({}))
    with pytest.raises(PiiRedactionError, match="system"):
        client.complete("prompt", SCHEMA, system=cast(str, ["tool", "spec"]))
    assert inner.calls == 0


# --- fail closed on the response (§8 placeholder integrity) -------------------------


@pytest.mark.parametrize("residue", ["<|PERSON_7|>", "<|person_1|>", "<|PERSON 1|>", "<|PERSON_"])
def test_placeholder_residue_in_the_response_fails_closed(residue: str) -> None:
    inner = FakeLLM(default={"relevance": 1, "reason": f"hire {residue}"})
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON"}))
    with pytest.raises(PiiRedactionError):
        client.complete(f"about {NAME}", SCHEMA)


def test_an_omitted_placeholder_is_not_an_error() -> None:
    inner = FakeLLM(default={"relevance": 1, "reason": "no names here"})
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON"}))
    assert client.complete(f"about {NAME}", SCHEMA).data["reason"] == "no names here"


# --- logging (§11) ------------------------------------------------------------------


def test_no_mapping_value_is_logged_on_success(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    inner = FakeLLM(default={"relevance": 1, "reason": "hire <|PERSON_1|>"})
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON", EMAIL: "EMAIL"}))
    client.complete(f"RESUME\n{NAME} <{EMAIL}>", SCHEMA)
    assert NAME not in caplog.text
    assert EMAIL not in caplog.text
    assert "<|PERSON_1|>" not in caplog.text


def test_no_mapping_value_is_logged_on_a_schema_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`openrouter_client.py` logs `last_error` on a schema failure; that message carries
    field names, never response content or a mapping value."""
    caplog.set_level(logging.DEBUG)
    completion = stub_completion(["not json at all", "still not json"])
    inner = LiteLLMClient(model="m", completion=completion)
    client = RedactingLLMClient(inner, redactor_for({NAME: "PERSON", EMAIL: "EMAIL"}))
    with pytest.raises(SchemaError):
        client.complete(f"RESUME\n{NAME} <{EMAIL}>", SCHEMA)
    assert "schema failure" in caplog.text
    assert NAME not in caplog.text
    assert EMAIL not in caplog.text
    assert "<|PERSON_1|>" not in caplog.text


# --- outbound monitoring (§10) ------------------------------------------------------


def test_outbound_warn_reports_only_categories(caplog: pytest.LogCaptureFixture) -> None:
    volunteered = "grace@navy.mil"
    inner = FakeLLM(default={"relevance": 1, "reason": f"contact {volunteered}"})
    client = RedactingLLMClient(
        inner,
        redactor_for({NAME: "PERSON", volunteered: "EMAIL"}),
        outbound="warn",
    )
    with caplog.at_level(logging.WARNING):
        response = client.complete(f"about {NAME}", SCHEMA)
    assert response.data["reason"] == f"contact {volunteered}"
    assert "EMAIL" in caplog.text
    assert volunteered not in caplog.text


def test_outbound_block_discards_a_response_with_unmapped_pii() -> None:
    volunteered = "grace@navy.mil"
    inner = FakeLLM(default={"relevance": 1, "reason": f"contact {volunteered}"})
    client = RedactingLLMClient(
        inner,
        redactor_for({volunteered: "EMAIL"}),
        outbound="block",
    )
    with pytest.raises(PiiRedactionError, match="EMAIL"):
        client.complete("ordinary prompt", SCHEMA)


def test_outbound_allow_does_not_scan() -> None:
    inner = FakeLLM(default={"relevance": 1, "reason": "contact grace@navy.mil"})
    client = RedactingLLMClient(inner, redactor_for({}), "allow")
    assert client.complete("ordinary prompt", SCHEMA).data["relevance"] == 1


def test_a_restored_request_value_is_not_unmapped_outbound_pii() -> None:
    inner = FakeLLM(default={"relevance": 1, "reason": "hire <|PERSON_1|>"})
    client = RedactingLLMClient(
        inner,
        redactor_for({NAME: "PERSON"}),
        outbound="block",
    )
    assert client.complete(f"about {NAME}", SCHEMA).data["reason"] == f"hire {NAME}"


def test_outbound_scanner_failure_warns_and_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailsAfterRequest:
        calls = 0

        def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("recall pass died")
            return [[] for _ in texts]

    inner = FakeLLM(default={"relevance": 1, "reason": "ok"})
    client = RedactingLLMClient(
        inner,
        Redactor(FailsAfterRequest(), ALL_ENTITIES),
        outbound="block",
    )
    with caplog.at_level(logging.WARNING):
        assert client.complete("ordinary prompt", SCHEMA).data["reason"] == "ok"
    assert "outbound PII scan unavailable" in caplog.text


# --- the factory (§13) --------------------------------------------------------------


def test_enabled_false_returns_no_redactor_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    assert build_redactor(Config(pii=PiiSpec(enabled=False))) is None
    assert "pii.enabled" in caplog.text


def test_the_default_spec_builds_a_redactor_over_the_configured_entities() -> None:
    redactor = build_redactor(Config())
    assert redactor is not None
    assert list(redactor.entities) == PiiSpec().entities


def test_building_a_redactor_loads_no_model() -> None:
    """`--dry-run` must cost nothing: the analyzer is built on first `detect()`, not here."""
    redactor = build_redactor(Config())
    assert redactor is not None
    detector = cast(Any, redactor.detector)
    assert detector._analyzer is None


# --- PERSON precision at the provider boundary (person-precision design §8) -------------

OWNER_RESUME = f"{NAME}\n{EMAIL}\nCo-architected a Kafka-driven path with Grafana dashboards."


def anchored_redactor() -> Redactor:
    """A redactor that has learned its owner from a resume, as the CLI wires it."""
    redactor = Redactor(
        SubstringDetector({NAME: "PERSON", EMAIL: "EMAIL", "Kafka": "PERSON"}),
        ALL_ENTITIES,
        policy=PersonPolicy(),
    )
    redactor.learn(OWNER_RESUME)
    return redactor


def test_the_prompt_reaching_the_provider_keeps_technical_names() -> None:
    inner = FakeLLM(default={"relevance": 1, "reason": "ok"})
    client = RedactingLLMClient(inner, anchored_redactor())
    client.complete(f"{OWNER_RESUME}\nThe role owns Kafka and Grafana.", SCHEMA)
    sent = inner.calls[0]
    assert "Kafka" in sent
    assert "Grafana" in sent
    assert NAME not in sent
    assert "<|PERSON_1|>" in sent


def test_restoration_still_round_trips_the_owner_name() -> None:
    inner = FakeLLM(default={"relevance": 4, "reason": "<|PERSON_1|> owns the Kafka edge"})
    client = RedactingLLMClient(inner, anchored_redactor())
    response = client.complete(f"{OWNER_RESUME}\nKafka role.", SCHEMA)
    assert response.data["reason"] == f"{NAME} owns the Kafka edge"
