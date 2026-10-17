from __future__ import annotations

from pathlib import Path

import pytest

from career_copilot.adapters.pii import factory
from career_copilot.config import Config, PiiSpec


def test_remote_recall_endpoint_requires_explicit_permission() -> None:
    with pytest.raises(ValueError, match="loopback"):
        factory.build_redactor(
            Config(pii=PiiSpec(llm_pass=True, llm_endpoint="https://example.com"))
        )


def test_disabled_redaction_does_not_check_the_recall_endpoint() -> None:
    assert factory.build_redactor(Config(pii=PiiSpec(enabled=False, llm_pass=True))) is None


def test_recall_readiness_is_checked_at_build_time(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[bool] = []

    class StubRecall:
        def __init__(self, endpoint: str, model: str, timeout_seconds: float) -> None:
            pass

        def check_ready(self) -> None:
            checked.append(True)

        def detect(self, texts: object) -> list[list[object]]:
            return []

    monkeypatch.setattr(factory, "OllamaDetector", StubRecall)
    assert factory.build_redactor(Config(pii=PiiSpec(llm_pass=True))) is not None
    assert checked == [True]


# --- PERSON precision policy (person-precision design §6) ------------------------------


def test_the_exemption_seed_includes_the_hard_filter_stack_terms() -> None:
    """The two lists cannot drift apart: anything scored as a skill cannot be redacted."""
    redactor = factory.build_redactor(Config())
    assert redactor is not None
    assert redactor.policy is not None
    assert "kafka" in redactor.policy.false_positive_terms
    assert "hugging face" in redactor.policy.false_positive_terms


def test_configured_stragglers_are_added_to_the_seed() -> None:
    config = Config(pii=PiiSpec(person_false_positive_terms=["Vector Search"]))
    redactor = factory.build_redactor(config)
    assert redactor is not None
    assert redactor.policy is not None
    assert "vector search" in redactor.policy.false_positive_terms


def test_disabling_the_owner_anchor_stops_derivation_without_disabling_the_filter() -> None:
    config = Config(pii=PiiSpec(owner_anchor=False))
    redactor = factory.build_redactor(config)
    assert redactor is not None
    assert redactor.policy is not None
    assert redactor.learn("Ada Lovelace ada@x.io", None).confidence == "none"


def test_an_absent_override_file_is_silent(tmp_path: object) -> None:
    config = Config(pii=PiiSpec(identity_path="does/not/exist.yaml"))
    assert factory.build_redactor(config) is not None


def test_a_malformed_override_file_raises_at_build_time(tmp_path: Path) -> None:
    path = tmp_path / "identity.yaml"
    path.write_text("names: 5\n", encoding="utf-8")
    config = Config(pii=PiiSpec(identity_path=str(path)))
    with pytest.raises(ValueError, match="identity"):
        factory.build_redactor(config)


def test_override_values_are_unioned_with_the_derived_ones(tmp_path: Path) -> None:
    path = tmp_path / "identity.yaml"
    path.write_text("names:\n  - Ada Byron\nphones:\n  - '+49 30 1234567'\n", encoding="utf-8")
    config = Config(pii=PiiSpec(identity_path=str(path)))
    redactor = factory.build_redactor(config)
    assert redactor is not None
    identity = redactor.identity()
    assert identity.names == ("Ada Byron",)
    assert identity.phones == ("+49 30 1234567",)
    assert identity.confidence == "confirmed"
