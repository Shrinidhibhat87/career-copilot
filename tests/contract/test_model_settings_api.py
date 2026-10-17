"""Settings round-trip, the key's write-only contract, and the OpenRouter stubs (spec 014)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import model_settings
from career_copilot.adapters.llm.openrouter import OpenRouterDirectory
from career_copilot.adapters.profiles import Hardware
from career_copilot.adapters.secrets import (
    ChainedSecretStore,
    EnvSecretStore,
    KeyringSecretStore,
    MemorySecretStore,
)
from career_copilot.config import Config
from career_copilot.usecases.model_settings import in_force
from tests.unit.test_secret_store import FakeKeyring, NoBackend

SHIPPED = Path(__file__).resolve().parents[2] / "deploy" / "profiles"
GOOD_KEY = "sk-or-v1-good-key-1234"

MODELS = {
    "data": [
        {
            "id": "google/gemini-3.8-flash",
            "name": "Gemini 3.8 Flash",
            "pricing": {"prompt": "0.00000075", "completion": "0.00000375"},
            "supported_parameters": ["tools", "response_format", "structured_outputs"],
        },
        {
            "id": "typesafe/jev-router",
            "name": "Jev Router",
            "pricing": {"prompt": "-1", "completion": "-1"},
            "supported_parameters": ["tools", "response_format"],
        },
        {
            "id": "plain/no-tools",
            "name": "No tools",
            "pricing": {"prompt": "0.0000001", "completion": "0.0000001"},
            "supported_parameters": ["response_format"],
        },
    ]
}


def openrouter(request: httpx.Request) -> Any:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json=MODELS)
    if request.url.path.endswith("/key"):
        if request.headers["Authorization"] == f"Bearer {GOOD_KEY}":
            return httpx.Response(200, json={"data": {"limit": 5.0, "usage": 0.25}})
        return httpx.Response(401, json={"error": "bad key"})
    return httpx.Response(404)


def unreachable(request: httpx.Request) -> Any:
    raise httpx.ConnectError("down", request=request)


class Setup:
    def __init__(self, tmp_path: Path, backend: FakeKeyring, handler=openrouter) -> None:  # type: ignore[no-untyped-def]
        shutil.copytree(SHIPPED, tmp_path / "deploy" / "profiles")
        (tmp_path / "config.yaml").write_text(f"db_path: {tmp_path / 'jobs.db'}\n")
        nvidia = Hardware("nvidia", "x")
        self.config = Config.load(tmp_path / "config.yaml", hardware=lambda: nvidia)
        self.env: dict[str, str] = {}
        self.backend = backend
        self.session = MemorySecretStore()
        app = FastAPI()
        app.include_router(
            model_settings.router(
                lambda: self.config,
                secrets=lambda: ChainedSecretStore(
                    EnvSecretStore(self.env), KeyringSecretStore(self.backend), self.session
                ),
                directory=OpenRouterDirectory(transport=httpx.MockTransport(handler)),
                env=self.env,
            )
        )
        self.client = TestClient(app)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path, FakeKeyring())


def test_nothing_saved_reads_as_the_machine_default(setup: Setup) -> None:
    body = setup.client.get("/api/settings/models").json()
    assert body["settings"]["preset"] is None
    assert body["key"] == {"set": False, "source": "none", "tail": None, "keychain": True}
    # Nothing saved: config.yaml as loaded, untouched.
    assert body["roles"][0]["kind"] == setup.config.role("triage").kind
    assert body["roles"][0]["source"] == "config"


def test_a_cloud_preset_needs_a_key_then_consent(setup: Setup) -> None:
    def put(body: dict[str, object]) -> Any:
        return setup.client.put("/api/settings/models", json=body)

    assert put({"preset": "cloud"}).status_code == 409
    setup.env["OPENROUTER_API_KEY"] = GOOD_KEY
    assert put({"preset": "cloud"}).status_code == 428
    saved = put({"preset": "cloud", "cloudConsent": True})
    assert saved.status_code == 200
    body = saved.json()
    assert body["settings"]["version"] == 1 and body["settings"]["cloudConsent"]
    assert all(r["kind"] == "openrouter" for r in body["roles"])
    assert body["profile"]["source"] == "settings"
    # Consent is asked once: a later save keeps it.
    assert put({"preset": "hybrid"}).json()["settings"]["version"] == 2


def test_local_preset_saves_without_key_or_consent(setup: Setup) -> None:
    response = setup.client.put("/api/settings/models", json={"preset": "local"})
    assert response.status_code == 200
    assert response.json()["roles"][0]["source"] == "preset"


def test_a_settings_change_applies_on_the_next_resolve(setup: Setup) -> None:
    setup.env["OPENROUTER_API_KEY"] = GOOD_KEY
    setup.client.put("/api/settings/models", json={"preset": "local"})
    assert in_force(setup.config, setup.env).role("match").kind == "vllm"
    setup.client.put("/api/settings/models", json={"preset": "hybrid", "cloudConsent": True})
    assert in_force(setup.config, setup.env).role("match").kind == "openrouter"


def test_overrides_pass_the_capability_gate_or_are_refused(setup: Setup) -> None:
    setup.env["OPENROUTER_API_KEY"] = GOOD_KEY

    def put(overrides: dict[str, object]) -> Any:
        return setup.client.put(
            "/api/settings/models",
            json={"preset": "local", "overrides": overrides, "cloudConsent": True},
        )

    refused = put({"assistant": {"backend": "openrouter", "model": "plain/no-tools"}})
    assert refused.status_code == 422
    assert "tools" in refused.json()["detail"]["errors"]["assistant"]
    missing = put({"match": {"backend": "openrouter", "model": "gone/model"}})
    assert missing.status_code == 422
    jev = put({"triage": {"backend": "openrouter", "model": "typesafe/jev-router"}})
    assert jev.status_code == 200
    assert jev.json()["settings"]["overrides"]["triage"]["reserveEur"] == 0.05
    roles = {r["role"]: r for r in jev.json()["roles"]}
    assert roles["triage"]["source"] == "override"


def test_the_key_is_write_only_and_tested_before_it_is_stored(setup: Setup) -> None:
    bad = setup.client.put("/api/settings/openrouter-key", json={"key": "sk-wrong-key-0000"})
    assert bad.status_code == 422 and setup.backend.saved == {}
    good = setup.client.put("/api/settings/openrouter-key", json={"key": GOOD_KEY})
    assert good.json() == {"set": True, "source": "keychain", "tail": "1234", "keychain": True}
    assert GOOD_KEY not in setup.client.get("/api/settings/openrouter-key").text
    assert GOOD_KEY not in setup.client.get("/api/settings/models").text
    cleared = setup.client.delete("/api/settings/openrouter-key").json()
    assert cleared["set"] is False and setup.backend.saved == {}


def test_key_test_reports_valid_invalid_and_unreachable(setup: Setup, tmp_path: Path) -> None:
    def test(key: str) -> Any:
        return setup.client.post("/api/settings/openrouter-key/test", json={"key": key})

    assert test(GOOD_KEY).json() == {"valid": True, "limit": 5.0, "usage": 0.25, "detail": ""}
    assert test("sk-wrong-key-0000").json()["valid"] is False
    assert setup.client.post("/api/settings/openrouter-key/test", json={}).status_code == 422
    down = Setup(tmp_path / "down", FakeKeyring(), unreachable)
    assert (
        down.client.post("/api/settings/openrouter-key/test", json={"key": GOOD_KEY}).status_code
        == 502
    )


def test_no_keychain_keeps_the_key_for_this_session(tmp_path: Path) -> None:
    setup = Setup(tmp_path, NoBackend())
    response = setup.client.put("/api/settings/openrouter-key", json={"key": GOOD_KEY})
    assert response.status_code == 200
    assert response.json() == {"set": True, "source": "session", "tail": "1234", "keychain": False}


def test_the_model_list_puts_the_catalog_first_then_gated_live_models(setup: Setup) -> None:
    body = setup.client.get("/api/settings/openrouter-models", params={"role": "assistant"}).json()
    tiers = [row["tier"] for row in body["catalog"]]
    assert tiers[0] == "recommended"
    flash = next(row for row in body["catalog"] if row["id"] == "google/gemini-3.8-flash")
    assert flash["priceIn"] == 0.75 and flash["available"] is True
    haiku = next(row for row in body["catalog"] if row["id"] == "anthropic/claude-haiku-4.5")
    assert haiku["available"] is False  # not in the stubbed live list
    others = [row["id"] for row in body["models"]]
    assert "plain/no-tools" not in others and "typesafe/jev-router" in others
    jev = next(row for row in body["models"] if row["id"] == "typesafe/jev-router")
    assert jev["dynamic"] is True
