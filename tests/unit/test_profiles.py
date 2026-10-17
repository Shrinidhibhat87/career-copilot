"""Model profiles: which machine this is, which profile that picks, and what it changes.

Every hardware fact is injected. No test here runs `nvidia-smi` or reads the real platform.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from career_copilot.adapters.profiles import (
    Hardware,
    ProfileChoice,
    ProfileError,
    apply_profile,
    available_profiles,
    choose_profile,
    detect_hardware,
    load_profile,
)
from career_copilot.config import Config

SHIPPED = Path(__file__).resolve().parents[2] / "deploy" / "profiles"
NAMES = {"mac-ollama", "nvidia-vllm-awq"}
MAC = Hardware("apple_silicon", "arm64")
NVIDIA = Hardware("nvidia", "NVIDIA GeForce GTX 1660 Ti")
OTHER = Hardware("other", "")


def nvidia_smi(returncode: int = 0, stdout: str = "NVIDIA GeForce GTX 1660 Ti\n"):  # type: ignore[no-untyped-def]
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def raising(exc: BaseException):  # type: ignore[no-untyped-def]
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise exc

    return run


# --- hardware ---------------------------------------------------------------


def test_macos_on_arm64_is_apple_silicon_and_picks_the_ollama_profile() -> None:
    hw = detect_hardware(
        run=raising(AssertionError("no nvidia-smi on a Mac")),
        system=lambda: "Darwin",
        machine=lambda: "arm64",
    )
    assert hw.kind == "apple_silicon"
    choice = choose_profile({}, "auto", hw, NAMES)
    assert choice == ProfileChoice("mac-ollama", "auto", "auto: Apple Silicon")


def test_windows_with_a_working_nvidia_smi_picks_the_vllm_profile() -> None:
    run = nvidia_smi()
    hw = detect_hardware(run=run, system=lambda: "Windows", machine=lambda: "AMD64")
    assert hw == Hardware("nvidia", "NVIDIA GeForce GTX 1660 Ti")
    assert run.calls[0][0] == "nvidia-smi"
    choice = choose_profile({}, "auto", hw, NAMES)
    assert choice.name == "nvidia-vllm-awq"
    assert choice.source == "auto"
    assert choice.reason == "auto: NVIDIA GPU found"


@pytest.mark.parametrize(
    "run",
    [
        raising(FileNotFoundError("nvidia-smi")),
        raising(subprocess.TimeoutExpired("nvidia-smi", 2)),
        raising(PermissionError("denied")),
        nvidia_smi(returncode=9, stdout=""),
    ],
    ids=["missing", "timed-out", "os-error", "non-zero"],
)
def test_no_usable_nvidia_smi_is_other_and_picks_no_profile(run) -> None:  # type: ignore[no-untyped-def]
    hw = detect_hardware(run=run, system=lambda: "Linux", machine=lambda: "x86_64")
    assert hw.kind == "other"
    choice = choose_profile({}, "auto", hw, NAMES)
    assert choice.name is None
    assert choice.source == "none"


def test_the_nvidia_probe_is_bounded_at_two_seconds() -> None:
    seen: dict[str, object] = {}

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="GPU\n", stderr="")

    detect_hardware(run=run, system=lambda: "Linux", machine=lambda: "x86_64")
    assert seen["timeout"] == 2


# --- order --------------------------------------------------------------------


def test_env_beats_config() -> None:
    env = {"CAREER_COPILOT_PROFILE": "mac-ollama"}
    choice = choose_profile(env, "nvidia-vllm-awq", NVIDIA, NAMES)
    assert (choice.name, choice.source) == ("mac-ollama", "env")


def test_config_beats_auto() -> None:
    choice = choose_profile({}, "mac-ollama", NVIDIA, NAMES)
    assert (choice.name, choice.source) == ("mac-ollama", "config")


def test_none_in_the_env_turns_profiles_off_even_over_config() -> None:
    choice = choose_profile({"CAREER_COPILOT_PROFILE": "none"}, "mac-ollama", MAC, NAMES)
    assert choice.name is None and choice.source == "none"


def test_none_in_the_config_turns_profiles_off_even_on_known_hardware() -> None:
    choice = choose_profile({}, "none", NVIDIA, NAMES)
    assert choice.name is None and choice.source == "none"


def test_auto_never_fails_when_its_profile_is_missing() -> None:
    choice = choose_profile({}, "auto", NVIDIA, {"mac-ollama"})
    assert choice.name is None and choice.source == "none"


def test_hardware_is_only_asked_for_when_auto_decides() -> None:
    def boom() -> Hardware:
        raise AssertionError("detected hardware although the env decided")

    choice = choose_profile({"CAREER_COPILOT_PROFILE": "mac-ollama"}, "auto", boom, NAMES)
    assert choice.name == "mac-ollama"


# --- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "configured"),
    [({"CAREER_COPILOT_PROFILE": "gpu-fast"}, "auto"), ({}, "gpu-fast")],
    ids=["env", "config"],
)
def test_an_unknown_name_lists_the_real_names(env: dict[str, str], configured: str) -> None:
    with pytest.raises(ProfileError) as err:
        choose_profile(env, configured, OTHER, NAMES)
    message = str(err.value)
    assert "gpu-fast" in message
    assert "mac-ollama" in message and "nvidia-vllm-awq" in message


def test_a_file_that_does_not_parse_is_named(tmp_path: Path) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text("models: {backends: [unclosed\n")
    with pytest.raises(ProfileError, match=r"broken\.yaml"):
        load_profile(bad)


def test_a_file_that_does_not_validate_is_named(tmp_path: Path) -> None:
    bad = tmp_path / "wrong.yaml"
    bad.write_text("models:\n  backends:\n    vllm: {kind: tpu}\n")
    with pytest.raises(ProfileError, match=r"wrong\.yaml"):
        load_profile(bad)


# --- merge --------------------------------------------------------------------


BASE: dict[str, object] = {
    "scoring": {"initial_target": 30, "max_eur_per_run": 2.5},
    "filters": {"preferred_cities": ["berlin"]},
    "discovery": {"max_searches": 4},
    "models": {
        "backends": {
            "ollama": {"kind": "ollama", "base_url": "http://localhost:11434"},
            "vllm": {"kind": "vllm", "base_url": "http://localhost:8000/v1"},
        },
        "roles": {
            "triage": {"backend": "ollama", "model": "qwen3:4b-instruct"},
            "match": {"backend": "ollama", "model": "qwen3:14b"},
        },
    },
}
OVERLAY = {
    "models": {
        "backends": {"vllm": {"kind": "vllm", "base_url": "http://localhost:8001/v1"}},
        "roles": {"triage": {"backend": "vllm", "model": "qwen3-4b-instruct"}},
    }
}


def test_merge_replaces_named_roles_and_backends_and_keeps_the_rest() -> None:
    merged = apply_profile(BASE, OVERLAY)
    models = merged["models"]
    assert models["backends"]["vllm"]["base_url"] == "http://localhost:8001/v1"
    assert models["backends"]["ollama"] == BASE["models"]["backends"]["ollama"]  # type: ignore[index]
    assert models["roles"]["triage"] == {"backend": "vllm", "model": "qwen3-4b-instruct"}
    assert models["roles"]["match"] == {"backend": "ollama", "model": "qwen3:14b"}
    for key in ("scoring", "filters", "discovery"):
        assert merged[key] == BASE[key]


def test_merge_does_not_mutate_its_input() -> None:
    apply_profile(BASE, OVERLAY)
    assert BASE["models"]["backends"]["vllm"]["base_url"] == "http://localhost:8000/v1"  # type: ignore[index]


def test_merge_into_a_config_without_a_models_block() -> None:
    merged = apply_profile({"scoring": {"initial_target": 9}}, OVERLAY)
    assert merged["models"]["roles"]["triage"]["model"] == "qwen3-4b-instruct"
    assert merged["scoring"] == {"initial_target": 9}


# --- Config.load end to end -------------------------------------------------


def write_project(tmp_path: Path, config: str = "") -> Path:
    profiles = tmp_path / "deploy" / "profiles"
    profiles.mkdir(parents=True)
    for shipped in SHIPPED.glob("*.yaml"):
        (profiles / shipped.name).write_text(shipped.read_text())
    path = tmp_path / "config.yaml"
    path.write_text(config or "scoring:\n  initial_target: 30\n")
    return path


def test_config_load_applies_the_auto_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAREER_COPILOT_PROFILE", raising=False)
    path = write_project(tmp_path)
    config = Config.load(path, hardware=lambda: NVIDIA)
    assert config.profile == ProfileChoice("nvidia-vllm-awq", "auto", "auto: NVIDIA GPU found")
    triage = config.role("triage")
    assert (triage.kind, triage.model, triage.base_url) == (
        "vllm",
        "qwen3-4b-instruct",
        "http://localhost:8001/v1",
    )
    assert config.scoring.initial_target == 30


def test_config_load_with_profiles_off_uses_the_file_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAREER_COPILOT_PROFILE", raising=False)
    path = write_project(tmp_path, "models:\n  profile: none\n")
    config = Config.load(path, hardware=lambda: NVIDIA)
    assert config.profile.name is None and config.profile.source == "none"
    assert config.models.roles == {}


def test_config_load_on_unknown_hardware_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAREER_COPILOT_PROFILE", raising=False)
    path = write_project(tmp_path)
    config = Config.load(path, hardware=lambda: OTHER)
    assert config.profile.name is None
    assert config.models.roles == {}


def test_config_load_stops_on_an_unknown_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAREER_COPILOT_PROFILE", "gpu-fast")
    path = write_project(tmp_path)
    with pytest.raises(ProfileError, match="mac-ollama"):
        Config.load(path, hardware=lambda: OTHER)


def test_a_detection_crash_counts_as_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAREER_COPILOT_PROFILE", raising=False)
    path = write_project(tmp_path)

    def broken() -> Hardware:
        raise RuntimeError("platform module exploded")

    assert Config.load(path, hardware=broken).profile.name is None


# --- shipped files ------------------------------------------------------------


def test_the_shipped_profiles_are_the_two_local_ones_and_the_cloud_one() -> None:
    # The cloud profile is never an auto pick; tests/unit/test_model_catalog.py covers it.
    assert available_profiles(SHIPPED) == NAMES | {"cloud-openrouter"}


@pytest.mark.parametrize("name", sorted(NAMES))
def test_each_shipped_profile_sets_every_role_with_room_for_the_agents(name: str) -> None:
    overlay = load_profile(SHIPPED / f"{name}.yaml")
    roles = overlay["models"]["roles"]
    assert set(roles) == {"triage", "match", "profiler", "curator", "assistant"}
    for role in ("profiler", "curator", "assistant"):
        assert roles[role]["max_output_tokens"] == 1024
    # The assistant alone may use a bigger model: a 4B one answers "done" without calling
    # its tool, and the mac profile has the memory for a better one.
    for role in ("profiler", "curator"):
        assert roles[role]["model"] == roles["triage"]["model"]
    if name != "mac-ollama":
        assert roles["assistant"]["model"] == roles["triage"]["model"]


def test_the_mac_profile_keeps_todays_values() -> None:
    roles = load_profile(SHIPPED / "mac-ollama.yaml")["models"]["roles"]
    assert roles["triage"] == {
        "backend": "ollama",
        "model": "qwen3:4b-instruct",
        "max_output_tokens": 256,
    }
    assert roles["match"] == {
        "backend": "ollama",
        "model": "qwen3:30b-a3b-instruct-2507-q4_K_M",
        "max_output_tokens": 1024,
    }


def test_the_nvidia_profile_uses_the_compose_port_not_the_ui_port() -> None:
    overlay = load_profile(SHIPPED / "nvidia-vllm-awq.yaml")["models"]
    assert overlay["backends"]["vllm"]["base_url"] == "http://localhost:8001/v1"
    assert {role["model"] for role in overlay["roles"].values()} == {"qwen3-4b-instruct"}
