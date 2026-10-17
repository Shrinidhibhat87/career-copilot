from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from career_copilot.adapters.profiles import Hardware
from career_copilot.config import Config, resolve_models
from career_copilot.domain.model_settings import ModelSettings, RoleOverride
from career_copilot.usecases.doctor import diagnose

SHIPPED = Path(__file__).resolve().parents[2] / "deploy" / "profiles"
APPLE = Hardware("apple_silicon", "arm64")
NVIDIA = Hardware("nvidia", "GTX 1660 Ti")
OTHER = Hardware("other", "")


def load(tmp_path: Path, hardware: Hardware, yaml: str = "db_path: x.db\n") -> Config:
    shutil.copytree(SHIPPED, tmp_path / "deploy" / "profiles")
    (tmp_path / "config.yaml").write_text(yaml)
    return Config.load(tmp_path / "config.yaml", hardware=lambda: hardware)


def kinds(config: Config) -> dict[str, str]:
    roles = ("triage", "match", "profiler", "curator", "assistant", "discovery")
    return {name: f"{config.role(name).kind}:{config.role(name).model}" for name in roles}  # type: ignore[arg-type]


def test_nothing_saved_returns_the_loaded_config(tmp_path: Path) -> None:
    config = load(tmp_path, NVIDIA)
    assert resolve_models(config, None, {}) is config
    assert resolve_models(config, ModelSettings(), {}) is config


@pytest.mark.parametrize(
    ("hardware", "triage"),
    [(APPLE, "ollama:qwen3:4b-instruct"), (NVIDIA, "vllm:qwen3-4b-instruct")],
)
def test_local_is_the_machine_pick(tmp_path: Path, hardware: Hardware, triage: str) -> None:
    resolved = resolve_models(load(tmp_path, hardware), ModelSettings(preset="local"), {})
    assert kinds(resolved)["triage"] == triage
    assert resolved.profile.source == "settings"


def test_cloud_puts_every_role_on_openrouter(tmp_path: Path) -> None:
    resolved = resolve_models(load(tmp_path, OTHER), ModelSettings(preset="cloud"), {})
    assert all(value.startswith("openrouter:") for value in kinds(resolved).values())
    assert resolved.role("triage").model == "deepseek/deepseek-v4-flash-0731"
    assert resolved.role("triage").max_output_tokens == 256


def test_hybrid_keeps_triage_local(tmp_path: Path) -> None:
    resolved = resolve_models(load(tmp_path, NVIDIA), ModelSettings(preset="hybrid"), {})
    found = kinds(resolved)
    assert found["triage"] == "vllm:qwen3-4b-instruct"
    for role in ("match", "profiler", "curator", "assistant", "discovery"):
        assert found[role].startswith("openrouter:")


def test_an_override_wins_and_survives_a_preset_change(tmp_path: Path) -> None:
    config = load(tmp_path, NVIDIA)
    override = {"match": RoleOverride("openrouter", "anthropic/claude-sonnet-5.5")}
    for preset in ("local", "cloud", "hybrid"):
        resolved = resolve_models(config, ModelSettings(preset=preset, overrides=override), {})
        assert kinds(resolved)["match"] == "openrouter:anthropic/claude-sonnet-5.5"
        assert resolved.role("match").max_output_tokens == 1024


def test_an_override_reserve_reaches_the_role(tmp_path: Path) -> None:
    override = {"triage": RoleOverride("openrouter", "typesafe/jev-router", 0.05)}
    resolved = resolve_models(load(tmp_path, OTHER), ModelSettings(overrides=override), {})
    assert resolved.role("triage").reserve_eur == 0.05


def test_the_env_profile_beats_a_saved_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"CAREER_COPILOT_PROFILE": "mac-ollama"}
    monkeypatch.setenv("CAREER_COPILOT_PROFILE", "mac-ollama")
    config = load(tmp_path, NVIDIA)
    resolved = resolve_models(config, ModelSettings(preset="cloud"), env)
    assert resolved.role("triage").kind != "openrouter"
    assert resolved.profile.source != "settings"


def test_a_saved_preset_beats_models_profile(tmp_path: Path) -> None:
    config = load(tmp_path, NVIDIA, "db_path: x.db\nmodels: {profile: mac-ollama}\n")
    resolved = resolve_models(config, ModelSettings(preset="cloud"), {})
    assert resolved.role("match").kind == "openrouter"
    assert resolved.profile.source == "settings"


def test_resolving_keeps_the_rest_of_the_config(tmp_path: Path) -> None:
    config = load(tmp_path, NVIDIA)
    before = config.role("match")
    config.company_lists.csv.path = "companies.csv"
    resolved = resolve_models(config, ModelSettings(preset="local"), {})
    assert resolved.company_lists.csv.path == "companies.csv"
    assert resolved.role("match").kind == "vllm"
    assert config.role("match") == before


def test_a_triage_model_without_tools_stops_the_curator_fallback(tmp_path: Path) -> None:
    config = load(
        tmp_path,
        OTHER,
        "db_path: x.db\nmodels:\n  profile: none\n  roles:\n"
        "    triage: {backend: ollama, model: tiny}\n",
    )
    from career_copilot.adapters.llm.backends import BackendStatus

    def prober(role):  # type: ignore[no-untyped-def]
        return BackendStatus(role.name, role.kind, role.model, None, True, True, tools=False)

    report = diagnose(config, prober)
    curator = next(s for s in report.statuses if s.role == "curator")
    assert not curator.usable
    assert "models.roles.curator" in curator.detail
    assert next(s for s in report.statuses if s.role == "triage").usable
