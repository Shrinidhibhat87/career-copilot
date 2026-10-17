"""`doctor` names the active model profile and where the choice came from.

The hardware and every backend are faked: the output must not depend on this machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import career_copilot.__main__ as cli
import career_copilot.config as config_module
from career_copilot.adapters.llm.backends import BackendStatus
from career_copilot.adapters.profiles import Hardware
from career_copilot.config import Config, ResolvedRole
from career_copilot.usecases import doctor as doctor_usecase
from tests.accounts_helper import make_account


@pytest.fixture(autouse=True)
def account(tmp_path: Path) -> None:
    make_account(tmp_path)


SHIPPED = Path(__file__).resolve().parents[2] / "deploy" / "profiles"


def down(role: ResolvedRole) -> BackendStatus:
    return BackendStatus(
        role=role.name,
        kind=role.kind,
        model=role.model,
        base_url=role.base_url,
        reachable=False,
        model_present=False,
        detail="unreachable",
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profiles = tmp_path / "deploy" / "profiles"
    profiles.mkdir(parents=True)
    for shipped in SHIPPED.glob("*.yaml"):
        (profiles / shipped.name).write_text(shipped.read_text())
    path = tmp_path / "config.yaml"
    path.write_text(f"db_path: {tmp_path / 'jobs.db'}\n")

    def fake_diagnose(config: Config, *args: object, **kwargs: object) -> object:
        return doctor_usecase.diagnose(config, down)

    monkeypatch.setattr(cli, "diagnose", fake_diagnose)
    monkeypatch.setattr(config_module, "detect_hardware", lambda: Hardware("nvidia", "GTX"))
    monkeypatch.delenv("CAREER_COPILOT_PROFILE", raising=False)
    return path


def doctor(path: Path) -> str:
    return CliRunner().invoke(cli.app, ["doctor", "--config", str(path)]).output


def test_auto(project: Path) -> None:
    assert "Profile: nvidia-vllm-awq (auto: NVIDIA GPU found)" in doctor(project)


def test_env(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREER_COPILOT_PROFILE", "mac-ollama")
    assert "Profile: mac-ollama (env: CAREER_COPILOT_PROFILE)" in doctor(project)


def test_config(project: Path) -> None:
    project.write_text(project.read_text() + "models:\n  profile: mac-ollama\n")
    assert "Profile: mac-ollama (config: models.profile)" in doctor(project)


def test_none(project: Path) -> None:
    project.write_text(project.read_text() + "models:\n  profile: none\n")
    assert "Profile: none (config.yaml as written)" in doctor(project)


def test_a_down_local_backend_prints_its_start_command(project: Path) -> None:
    output = doctor(project)
    assert "start it: docker compose -f deploy/vllm/docker-compose.yml up" in output
    # One hint per backend, not one per role.
    assert output.count("start it:") == 1


def test_an_unknown_profile_stops_with_the_real_names(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAREER_COPILOT_PROFILE", "gpu-fast")
    result = CliRunner().invoke(cli.app, ["doctor", "--config", str(project)])
    assert result.exit_code != 0
    assert "mac-ollama" in str(result.exception) + result.output
