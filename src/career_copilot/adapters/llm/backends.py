"""Turning a configured role into a client, and asking a backend whether it is there.

This is the only module that knows a litellm prefix or a model server's health URL.
`usecases` sees `ResolvedRole` and `BackendStatus`; neither mentions a provider.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from career_copilot.adapters.llm.litellm_client import LiteLLMClient
from career_copilot.adapters.model_catalog import load_catalog
from career_copilot.adapters.secrets import default_secrets, secret_for_env
from career_copilot.config import ResolvedRole
from career_copilot.ports import LLMClient, Recorder, SecretStore

PROBE_TIMEOUT = 3.0

# Local servers accept any string; litellm insists on one being present.
LOCAL_API_KEY = "local"


class BackendUnavailable(RuntimeError):
    """A role's backend cannot serve it. Raised before a run starts, never during."""


@dataclass(frozen=True, slots=True)
class BackendStatus:
    """What a probe found. `tools` is tri-state: a backend that does not say is None."""

    role: str
    kind: str
    model: str
    base_url: str | None
    reachable: bool
    model_present: bool
    tools: bool | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.reachable and self.model_present


Prober = Callable[[ResolvedRole], BackendStatus]


def role_key(role: ResolvedRole, secrets: SecretStore | None = None) -> str | None:
    """The API key a remote role needs: env first, then the OS keychain. None if unset."""
    if not role.key_env:
        return None
    name = secret_for_env(role.key_env)
    return (secrets or default_secrets()).get(name) if name else None


def build_client(
    role: ResolvedRole,
    recorder: Recorder | None = None,
    run_id: int | None = None,
    secrets: SecretStore | None = None,
) -> LLMClient:
    """One client for one role. Missing key for a remote role fails here, not mid-run."""
    api_key = None
    if role.key_env:
        api_key = role_key(role, secrets)
        if not api_key:
            raise BackendUnavailable(
                f"role '{role.name}' needs an OpenRouter key: save one on the Settings page "
                f"or set {role.key_env} in .env "
                f"(or point it at a local backend in models.roles.{role.name})"
            )
    else:
        api_key = LOCAL_API_KEY
    return LiteLLMClient(
        model=role.litellm_model,
        base_url=role.base_url,
        api_key=api_key,
        max_tokens=role.max_output_tokens,
        recorder=recorder,
        stage=role.name,
        backend=role.kind,
        run_id=run_id,
    )


def _ollama_status(role: ResolvedRole, client: httpx.Client) -> BackendStatus:
    base = (role.base_url or "http://localhost:11434").rstrip("/")
    response = client.get(f"{base}/api/tags", timeout=PROBE_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    names = {str(entry.get("name", "")) for entry in payload.get("models", [])}
    # Ollama reports `qwen3:4b`; a config that omits the tag means `:latest`.
    wanted = role.model if ":" in role.model else f"{role.model}:latest"
    present = wanted in names or role.model in names
    tools: bool | None = None
    detail = "" if present else f"model not pulled — run: ollama pull {role.model}"
    if present:
        tools = _ollama_supports_tools(base, role.model, client)
    return BackendStatus(
        role=role.name,
        kind=role.kind,
        model=role.model,
        base_url=base,
        reachable=True,
        model_present=present,
        tools=tools,
        detail=detail,
    )


def _ollama_supports_tools(base: str, model: str, client: httpx.Client) -> bool | None:
    """Older Ollama builds do not report capabilities. Unknown is not the same as no."""
    try:
        response = client.post(f"{base}/api/show", json={"model": model}, timeout=PROBE_TIMEOUT)
        response.raise_for_status()
        capabilities = response.json().get("capabilities")
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(capabilities, list):
        return None
    return "tools" in capabilities


def _openai_compatible_status(role: ResolvedRole, client: httpx.Client) -> BackendStatus:
    base = (role.base_url or "http://localhost:8000/v1").rstrip("/")
    response = client.get(f"{base}/models", timeout=PROBE_TIMEOUT)
    response.raise_for_status()
    ids = {str(entry.get("id", "")) for entry in response.json().get("data", [])}
    present = role.model in ids
    return BackendStatus(
        role=role.name,
        kind=role.kind,
        model=role.model,
        base_url=base,
        reachable=True,
        model_present=present,
        detail="" if present else f"served models: {', '.join(sorted(ids)) or 'none'}",
    )


def probe(
    role: ResolvedRole, client: httpx.Client | None = None, secrets: SecretStore | None = None
) -> BackendStatus:
    """Ask a backend what it has. Never raises: an unreachable backend is a report."""
    if role.kind == "openrouter":
        # A remote probe would cost a request and tell us nothing a key check does not.
        has_key = bool(role_key(role, secrets))
        # Tool support is known for catalog models; for any other it is not claimed.
        entry = load_catalog().entry(role.model)
        return BackendStatus(
            role=role.name,
            kind=role.kind,
            model=role.model,
            base_url=None,
            reachable=has_key,
            model_present=has_key,
            tools=("tools" in entry.caps) if entry else None,
            detail="" if has_key else f"no OpenRouter key ({role.key_env} or Settings)",
        )
    owned = client is None
    http = client or httpx.Client()
    try:
        if role.kind == "ollama":
            return _ollama_status(role, http)
        return _openai_compatible_status(role, http)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return BackendStatus(
            role=role.name,
            kind=role.kind,
            model=role.model,
            base_url=role.base_url,
            reachable=False,
            model_present=False,
            detail=f"{role.base_url} unreachable ({type(exc).__name__})",
        )
    finally:
        if owned:
            http.close()
