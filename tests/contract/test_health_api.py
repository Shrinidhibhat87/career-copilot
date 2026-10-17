"""`GET /api/health/models`: the same readiness the CLI prints, for the settings screen."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import health
from career_copilot.adapters.llm.backends import probe
from career_copilot.adapters.profiles import ProfileChoice
from career_copilot.config import BackendSpec, Config, ModelsSpec, RoleSpec

LOCAL = Config(
    models=ModelsSpec(
        backends={"ollama": BackendSpec(kind="ollama", base_url="http://localhost:11434")},
        roles={
            "triage": RoleSpec(backend="ollama", model="qwen3:4b"),
            "match": RoleSpec(backend="ollama", model="qwen3:14b"),
        },
    )
)


def client_for(handler: object | None = None) -> TestClient:
    app = FastAPI()
    if handler is None:
        app.include_router(health.router(lambda: LOCAL))
    else:
        http = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
        app.include_router(health.router(lambda: LOCAL, lambda role: probe(role, http)))
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return client_for()


def test_an_unreachable_backend_is_reported_per_role(client: TestClient) -> None:
    body = client.get("/api/health/models").json()
    assert body["ok"] is False
    assert [role["role"] for role in body["roles"]] == [
        "triage",
        "match",
        "profiler",
        "curator",
        "assistant",
    ]
    assert body["roles"][0]["kind"] == "ollama"
    assert body["roles"][0]["reachable"] is False


def test_a_ready_backend_answers_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "qwen3:4b"}, {"name": "qwen3:14b"}]}
            )
        return httpx.Response(200, json={"capabilities": ["tools"]})

    body = client_for(handler).get("/api/health/models").json()
    assert body["ok"] is True
    assert body["roles"][0]["tools"] is True


def test_the_active_profile_is_named_with_its_source() -> None:
    profiled = LOCAL.model_copy(
        update={"profile": ProfileChoice("mac-ollama", "env", "env: CAREER_COPILOT_PROFILE")}
    )
    app = FastAPI()
    app.include_router(health.router(lambda: profiled))
    body = TestClient(app).get("/api/health/models").json()
    assert body["profile"] == {"name": "mac-ollama", "source": "env"}


def test_no_profile_is_reported_as_none(client: TestClient) -> None:
    assert client.get("/api/health/models").json()["profile"] == {"name": None, "source": "none"}


@pytest.mark.parametrize(
    ("kind", "local"), [("ollama", True), ("vllm", True), ("openrouter", False)]
)
def test_each_role_says_whether_it_runs_locally(kind: str, local: bool) -> None:
    config = Config(
        models=ModelsSpec(
            backends={
                "ollama": BackendSpec(kind="ollama", base_url="http://localhost:11434"),
                "other": BackendSpec(kind=kind),  # type: ignore[arg-type]
            },
            roles={
                "triage": RoleSpec(backend="ollama", model="qwen3:4b"),
                "match": RoleSpec(backend="other", model="some-model"),
            },
        )
    )
    app = FastAPI()
    app.include_router(health.router(lambda: config))
    roles = TestClient(app).get("/api/health/models").json()["roles"]
    local_by_role = {role["role"]: role["local"] for role in roles}
    assert (local_by_role["triage"], local_by_role["match"]) == (True, local)
