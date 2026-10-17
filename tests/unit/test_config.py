from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from career_copilot.config import Config


def test_scoring_defaults_are_present() -> None:
    config = Config()
    assert config.initial_target == 50
    assert config.scoring.initial_target == 50
    assert config.scoring.incremental_target == 15
    assert config.scoring.triage_model == "openrouter/google/gemini-2.5-flash"
    assert config.scoring.match_model == "openrouter/google/gemini-2.5-pro"
    assert config.scoring.triage_max_output_tokens == 256
    assert config.scoring.match_max_output_tokens == 1024
    assert config.scoring.triage_reserve_eur > 0
    assert config.scoring.match_reserve_eur > 0
    assert config.scoring.rubric_version == 3
    assert config.scoring.digest_path == "data/digest.md"


def test_scoring_block_is_read_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "initial_target: 25\n"
        "scoring:\n"
        "  initial_target: 30\n"
        "  incremental_target: 4\n"
        "  triage_model: cheap-model\n"
        "  match_model: strong-model\n"
        "  max_eur_per_run: 2.5\n"
    )
    config = Config.load(path)
    assert config.initial_target == 25
    assert config.scoring.initial_target == 30
    assert config.scoring.incremental_target == 4
    assert config.scoring.triage_model == "cheap-model"
    assert config.scoring.match_model == "strong-model"
    assert config.scoring.max_eur_per_run == 2.5


def test_a_missing_config_still_loads_defaults(tmp_path: Path) -> None:
    assert Config.load(tmp_path / "nope.yaml").scoring.incremental_target == 15


def test_production_cost_guards_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"scoring": {"max_eur_per_run": 0}})


def test_relevance_and_diversity_keys_are_read(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "filters:\n  excluded_role_terms: [mobile]\n  core_stack_terms: [rust]\n"
        "  excluded_stack_terms: [php]\n  max_per_company: 3\n"
    )
    config = Config.load(path).filters.to_filter_config()
    assert config.excluded_role_terms == ("mobile",)
    assert config.core_stack_terms == ("rust",)
    assert config.excluded_stack_terms == ("php",)
    assert config.max_per_company == 3


def test_old_filter_config_gets_relevance_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("filters:\n  allowed_cities: [berlin]\n")
    config = Config.load(path).filters.to_filter_config()
    assert "python" in config.core_stack_terms
    assert config.max_per_company == 2


def test_pii_defaults_match_the_design_doc() -> None:
    pii = Config().pii
    assert pii.enabled is True
    assert pii.entities == [
        "PERSON",
        "EMAIL",
        "PHONE",
        "ADDRESS",
        "URL",
        "IBAN",
        "CREDIT_CARD",
        "IP",
    ]
    assert pii.llm_pass is False
    assert pii.llm_endpoint == "http://localhost:11434"
    assert pii.llm_allow_remote is False
    assert pii.language == "en"
    assert pii.llm_model == "qwen2.5:3b"
    assert pii.llm_timeout_seconds == 30
    assert pii.llm_failure == "block"
    assert pii.outbound == "warn"


def test_an_omitted_pii_block_does_not_disable_redaction(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("initial_target: 10\n")
    config = Config.load(path)
    assert config.pii.enabled is True
    assert config.pii.llm_failure == "block"


def test_pii_block_is_read_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "pii:\n"
        "  enabled: false\n"
        "  entities: [EMAIL]\n"
        "  llm_pass: true\n"
        "  llm_failure: presidio_only\n"
        "  outbound: block\n"
    )
    config = Config.load(path)
    assert config.pii.enabled is False
    assert config.pii.entities == ["EMAIL"]
    assert config.pii.llm_pass is True
    assert config.pii.llm_failure == "presidio_only"
    assert config.pii.outbound == "block"


def test_pii_llm_failure_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"pii": {"llm_failure": "retry"}})


def test_pii_outbound_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"pii": {"outbound": "deny"}})
