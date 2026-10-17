"""Which machine is this, and which model profile does that pick?

A profile is a `models:` block in `deploy/profiles/<name>.yaml`. It overlays the same-named
backends and roles in `config.yaml`, so moving between the Mac and the NVIDIA laptop is
not an edit. The choice is always explainable: `ProfileChoice.reason` is what `doctor`
prints.
"""

from __future__ import annotations

import copy
import platform
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

ENV_VAR = "CAREER_COPILOT_PROFILE"
AUTO = "auto"
OFF = "none"
# Every role on OpenRouter (spec 014). Never picked by auto: only a preset or a name does.
CLOUD_PROFILE = "cloud-openrouter"
NVIDIA_SMI_TIMEOUT = 2

# What auto picks per hardware kind. `other` is absent on purpose: no profile at all.
_AUTO_PICKS = {
    "apple_silicon": ("mac-ollama", "auto: Apple Silicon"),
    "nvidia": ("nvidia-vllm-awq", "auto: NVIDIA GPU found"),
}


class ProfileError(ValueError):
    """A named profile that does not exist, or a profile file that is not valid."""


@dataclass(frozen=True)
class Hardware:
    kind: Literal["apple_silicon", "nvidia", "other"]
    detail: str  # "arm64" on a Mac, or the GPU name from nvidia-smi


@dataclass(frozen=True)
class ProfileChoice:
    name: str | None
    source: Literal["env", "settings", "config", "auto", "none"]
    reason: str  # shown by doctor


NO_PROFILE = ProfileChoice(None, "none", "config.yaml as written")


def detect_hardware(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    system: Callable[[], str] = platform.system,
    machine: Callable[[], str] = platform.machine,
) -> Hardware:
    """Apple Silicon by platform, NVIDIA by a working `nvidia-smi`, anything else is other."""
    if system() == "Darwin" and machine() == "arm64":
        return Hardware("apple_silicon", machine())
    try:
        result = run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Hardware("other", "")
    if result.returncode != 0:
        return Hardware("other", "")
    lines = (result.stdout or "").strip().splitlines()
    return Hardware("nvidia", lines[0].strip() if lines else "")


def choose_profile(
    env: Mapping[str, str],
    configured: str,
    hw: Hardware | Callable[[], Hardware],
    available: set[str],
) -> ProfileChoice:
    """Env, then `models.profile`, then auto. `hw` may be a callable, so the hardware is
    only probed when auto actually decides."""
    for value, source, reason in (
        (env.get(ENV_VAR, "").strip(), "env", f"env: {ENV_VAR}"),
        ((configured or AUTO).strip(), "config", "config: models.profile"),
    ):
        if not value or value == AUTO:
            continue
        if value == OFF:
            return NO_PROFILE
        if value not in available:
            names = ", ".join(sorted(available)) or "none"
            raise ProfileError(
                f"unknown model profile '{value}' (from {source}); available: {names}"
            )
        return ProfileChoice(value, source, reason)  # type: ignore[arg-type]
    hardware = hw() if callable(hw) else hw
    pick = _AUTO_PICKS.get(hardware.kind)
    if pick is None or pick[0] not in available:
        return NO_PROFILE
    return ProfileChoice(pick[0], "auto", pick[1])


def available_profiles(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {path.stem for path in directory.glob("*.yaml")}


def load_profile(path: Path) -> dict[str, Any]:
    """Read and validate one profile file. Any failure names the file."""
    # Imported here: config imports this module to run the profile flow.
    from career_copilot.config import ModelsSpec

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"model profile {path} does not parse: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ProfileError(f"model profile {path} must hold a `models:` block")
    try:
        ModelsSpec.model_validate(data["models"])
    except ValidationError as exc:
        raise ProfileError(f"model profile {path} is not valid: {exc}") from exc
    return data


def apply_profile(data: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Replace `models.backends[k]` and `models.roles[k]` for each k the profile names.

    Everything else in the config, including backends and roles the profile does not
    name, is left exactly as written.
    """
    merged = copy.deepcopy(dict(data))
    models = merged.setdefault("models", {}) or {}
    merged["models"] = models
    profile_models = overlay.get("models") or {}
    for section in ("backends", "roles"):
        entries = profile_models.get(section) or {}
        if not entries:
            continue
        target = models.get(section) or {}
        target.update(copy.deepcopy(entries))
        models[section] = target
    return merged
