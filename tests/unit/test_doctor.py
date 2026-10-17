"""The readiness report, over a fake prober. No process on this machine is contacted."""

from __future__ import annotations

from dataclasses import replace

from career_copilot.adapters.llm.backends import BackendStatus, Prober
from career_copilot.adapters.profiles import NO_PROFILE, ProfileChoice
from career_copilot.config import BackendSpec, Config, ModelsSpec, ResolvedRole, RoleSpec
from career_copilot.usecases.doctor import PiiStatus, diagnose, profile_line, start_hint

LOCAL = Config(
    models=ModelsSpec(
        backends={"ollama": BackendSpec(kind="ollama", base_url="http://localhost:11434")},
        roles={
            "triage": RoleSpec(backend="ollama", model="qwen3:4b"),
            "match": RoleSpec(backend="ollama", model="qwen3:14b"),
        },
    )
)


def prober(**overrides: bool) -> Prober:
    def probe(role: ResolvedRole) -> BackendStatus:
        reachable = overrides.get(f"{role.name}_reachable", True)
        present = overrides.get(f"{role.name}_present", True)
        return BackendStatus(
            role=role.name,
            kind=role.kind,
            model=role.model,
            base_url=role.base_url,
            reachable=reachable,
            model_present=present,
            detail="" if reachable and present else "not pulled",
        )

    return probe


FIVE = ["triage", "match", "profiler", "curator", "assistant"]


def test_every_role_is_reported_in_order() -> None:
    report = diagnose(LOCAL, prober())
    assert [status.role for status in report.statuses] == FIVE
    assert report.ok and report.problem() == ""


def test_one_missing_model_fails_the_report_and_names_the_role() -> None:
    report = diagnose(LOCAL, prober(match_present=False))
    assert not report.ok
    assert [status.role for status in report.unusable] == ["match"]
    assert "match → ollama:qwen3:14b (not pulled)" in report.problem()


def test_an_unreachable_server_fails_every_role_it_serves() -> None:
    report = diagnose(LOCAL, prober(triage_reachable=False, match_reachable=False))
    # profiler, curator and assistant fall back to triage's model, so they fail with it.
    assert [status.role for status in report.unusable] == FIVE


# --- profiles: five roles, probed once per backend and model --------------


def counting_prober() -> tuple[Prober, list[str]]:
    calls: list[str] = []
    inner = prober()

    def probe(role: ResolvedRole) -> BackendStatus:
        calls.append(f"{role.kind}:{role.base_url}:{role.model}")
        return inner(role)

    return probe, calls


PROFILED = Config(
    models=ModelsSpec(
        backends={"vllm": BackendSpec(kind="vllm", base_url="http://localhost:8001/v1")},
        roles={
            name: RoleSpec(backend="vllm", model="qwen3-4b-instruct", max_output_tokens=tokens)
            for name, tokens in zip(FIVE, (256, 1024, 1024, 1024, 1024), strict=True)
        },
    )
)


def test_five_roles_on_one_model_cost_one_probe() -> None:
    probe, calls = counting_prober()
    report = diagnose(PROFILED, probe)
    assert [status.role for status in report.statuses] == FIVE
    assert len(calls) == 1
    assert {status.model for status in report.statuses} == {"qwen3-4b-instruct"}


def test_roles_on_different_models_are_probed_separately() -> None:
    probe, calls = counting_prober()
    diagnose(LOCAL, probe)  # triage model shared by three agents, match on its own
    assert sorted(calls) == [
        "ollama:http://localhost:11434:qwen3:14b",
        "ollama:http://localhost:11434:qwen3:4b",
    ]


def test_the_report_carries_the_active_profile() -> None:
    choice = ProfileChoice("nvidia-vllm-awq", "auto", "auto: NVIDIA GPU found")
    report = diagnose(PROFILED.model_copy(update={"profile": choice}), prober())
    assert report.profile == choice
    assert profile_line(choice) == "Profile: nvidia-vllm-awq (auto: NVIDIA GPU found)"


def test_no_profile_says_the_config_is_used_as_written() -> None:
    assert diagnose(LOCAL, prober()).profile == NO_PROFILE
    assert profile_line(NO_PROFILE) == "Profile: none (config.yaml as written)"


def test_a_down_local_backend_comes_with_its_start_command() -> None:
    assert start_hint("ollama") == "ollama serve"
    assert start_hint("vllm") == "docker compose -f deploy/vllm/docker-compose.yml up"
    assert start_hint("openrouter") is None


# --- the redaction boundary -----------------------------------------------


def test_the_pii_probe_is_opt_in_so_the_cheap_checks_stay_cheap() -> None:
    """`diagnose` runs on every run start and on /api/health. Probing redaction loads
    spaCy, which `PresidioDetector` exists to defer, so it is asked for explicitly."""
    report = diagnose(LOCAL, prober())
    assert report.pii.detail == "not checked"
    assert report.ok


def test_an_unusable_redaction_boundary_fails_the_report() -> None:
    report = replace(diagnose(LOCAL, prober()), pii=PiiStatus(True, False, "model missing"))
    assert not report.ok
    assert "redaction → model missing" in report.problem()


def test_redaction_switched_off_is_not_a_failure() -> None:
    report = replace(diagnose(LOCAL, prober()), pii=PiiStatus(False, True, "redaction is off"))
    assert report.ok and report.problem() == ""


def _status(role: str, kind: str, usable: bool) -> BackendStatus:
    return BackendStatus(role, kind, "m", None, usable, usable)


def test_a_down_local_role_suggests_hybrid_with_a_key_and_add_key_without() -> None:
    from career_copilot.usecases.doctor import suggest_for

    down_match = (_status("triage", "vllm", True), _status("match", "vllm", False))
    assert suggest_for(down_match, key_set=True) == "hybrid"
    assert suggest_for(down_match, key_set=False) == "add_key"
    down_triage = (_status("triage", "ollama", False), _status("match", "ollama", False))
    assert suggest_for(down_triage, key_set=True) == "cloud"


def test_nothing_is_suggested_when_only_a_cloud_role_fails() -> None:
    from career_copilot.usecases.doctor import suggest_for

    assert suggest_for((_status("match", "openrouter", False),), key_set=False) is None
