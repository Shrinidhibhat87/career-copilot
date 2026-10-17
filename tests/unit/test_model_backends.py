"""Role resolution, client construction and backend probing.

Nothing here reaches a network: httpx is driven by a MockTransport, and the OpenRouter
probe is a key check by design.
"""

from __future__ import annotations

import httpx
import pytest

from career_copilot.adapters.llm.backends import (
    BackendUnavailable,
    build_client,
    probe,
)
from career_copilot.adapters.llm.litellm_client import LiteLLMClient
from career_copilot.config import (
    BackendSpec,
    Config,
    DiscoverySpec,
    ModelsSpec,
    RoleSpec,
    ScoringSpec,
)

LOCAL = ModelsSpec(
    backends={
        "ollama": BackendSpec(kind="ollama", base_url="http://localhost:11434"),
        "vllm": BackendSpec(kind="vllm", base_url="http://localhost:8000/v1"),
    },
    roles={
        "triage": RoleSpec(backend="ollama", model="qwen3:4b", max_output_tokens=256),
        "match": RoleSpec(backend="vllm", model="Qwen/Qwen3-4B-Instruct"),
    },
)


def client_returning(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_a_configless_machine_still_resolves_the_gemini_roles() -> None:
    role = Config().role("triage")
    assert (role.kind, role.litellm_model) == ("openrouter", "openrouter/google/gemini-2.5-flash")
    assert role.key_env == "OPENROUTER_API_KEY"
    assert not role.is_local


@pytest.mark.parametrize("field", ("max_eur_per_run", "max_seconds"))
def test_discovery_limits_cannot_be_disabled(field: str) -> None:
    with pytest.raises(ValueError):
        DiscoverySpec(**{field: 0})


def test_scoring_ids_still_carry_their_token_and_reserve_limits() -> None:
    config = Config(scoring=ScoringSpec(match_max_output_tokens=999, match_reserve_eur=0.5))
    role = config.role("match")
    assert (role.max_output_tokens, role.reserve_eur) == (999, 0.5)


def test_a_local_role_becomes_a_litellm_id_for_its_kind() -> None:
    config = Config(models=LOCAL)
    assert config.role("triage").litellm_model == "ollama_chat/qwen3:4b"
    assert config.role("match").litellm_model == "hosted_vllm/Qwen/Qwen3-4B-Instruct"
    assert config.role("triage").is_local


def test_a_local_role_needs_no_key() -> None:
    config = Config(models=LOCAL)
    assert config.role("triage").key_env is None
    client = build_client(config.role("triage"))
    assert isinstance(client, LiteLLMClient)
    assert client.model == "ollama_chat/qwen3:4b"


def test_a_remote_role_without_its_key_fails_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(BackendUnavailable, match="OPENROUTER_API_KEY"):
        build_client(Config().role("triage"))


def test_a_role_pointing_at_an_unknown_backend_is_a_config_error() -> None:
    config = Config(models=ModelsSpec(roles={"triage": RoleSpec(backend="ghost", model="m")}))
    with pytest.raises(ValueError, match=r"not in models\.backends"):
        config.role("triage")


def test_ollama_reports_a_pulled_model_and_its_tool_capability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]})
        return httpx.Response(200, json={"capabilities": ["completion", "tools"]})

    status = probe(Config(models=LOCAL).role("triage"), client_returning(handler))
    assert (status.usable, status.tools) == (True, True)


def test_ollama_says_how_to_pull_a_model_it_does_not_have() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]})

    status = probe(Config(models=LOCAL).role("triage"), client_returning(handler))
    assert not status.usable
    assert "ollama pull qwen3:4b" in status.detail


def test_a_tagless_model_name_matches_the_latest_tag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:latest"}]})
        return httpx.Response(200, json={})

    models = ModelsSpec(roles={"triage": RoleSpec(backend="ollama", model="qwen3")})
    status = probe(Config(models=models).role("triage"), client_returning(handler))
    assert status.model_present


def test_an_older_ollama_that_reports_no_capabilities_is_unknown_not_no() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]})
        return httpx.Response(404)

    status = probe(Config(models=LOCAL).role("triage"), client_returning(handler))
    assert (status.usable, status.tools) == (True, None)


def test_vllm_is_read_through_the_openai_model_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-4B-Instruct"}]})

    status = probe(Config(models=LOCAL).role("match"), client_returning(handler))
    assert status.usable


def test_a_served_model_that_is_not_the_configured_one_is_named() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other/model"}]})

    status = probe(Config(models=LOCAL).role("match"), client_returning(handler))
    assert not status.usable
    assert "other/model" in status.detail


def test_a_server_that_is_not_running_is_a_report_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    status = probe(Config(models=LOCAL).role("triage"), client_returning(handler))
    assert (status.reachable, status.model_present) == (False, False)
    assert "unreachable" in status.detail


def test_the_openrouter_probe_costs_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    status = probe(Config().role("match"))
    assert status.usable and status.tools is True
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert not probe(Config().role("match")).usable
