"""What can this machine actually run?

A local runtime moves the common failure from "the key is wrong" to "the server is not
up, or the model was never pulled". Both are cheap to check and expensive to discover
halfway through a scoring run, so they are checked before one starts and reported on
demand.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from career_copilot.adapters.llm.backends import BackendStatus, Prober, probe
from career_copilot.adapters.profiles import NO_PROFILE, ProfileChoice
from career_copilot.adapters.secrets import OPENROUTER, default_secrets
from career_copilot.config import Config
from career_copilot.ports import PiiRedactionError, SecretStore

ROLES: tuple[Literal["triage", "match", "profiler", "curator", "assistant"], ...] = (
    "triage",
    "match",
    "profiler",
    "curator",
    "assistant",
)

# Roles that fall back to the triage model unless set, and must call tools to work.
_TOOL_ROLES = ("curator", "assistant")

# What the page offers when a local backend is down (spec 014). Never applied for the user.
Suggest = Literal["hybrid", "cloud", "add_key"]

# The app never starts a model server. When one is down, say how to start it; these are
# the commands in deploy/README.md.
_START_COMMANDS = {
    "ollama": "ollama serve",
    "vllm": "docker compose -f deploy/vllm/docker-compose.yml up",
}


def start_hint(kind: str) -> str | None:
    return _START_COMMANDS.get(kind)


def profile_line(choice: ProfileChoice) -> str:
    """The one line `doctor` and `ui` print about where the model setup came from."""
    return f"Profile: {choice.name or 'none'} ({choice.reason})"


# Short, synthetic, and deliberately PII-bearing: the probe has to exercise the same
# detection path a real call takes, and must never read the user's own documents to do it.
_PROBE_TEXT = "Ada Lovelace, ada@example.com"


@dataclass(frozen=True, slots=True)
class PiiStatus:
    """Whether the redaction boundary can actually run.

    A missing spaCy model builds a `Redactor` happily and only fails on the first
    `detect()`, which is deep inside a scoring run or an upload — so the probe redacts one
    synthetic string here instead of discovering it there.
    """

    enabled: bool
    usable: bool
    detail: str = ""


def probe_pii(config: Config) -> PiiStatus:
    # Imported here, not at module scope: the CLI imports this module to print a version,
    # and building the redactor pulls in Presidio.
    from career_copilot.adapters.pii.factory import build_redactor

    try:
        redactor = build_redactor(config)
    except Exception as exc:
        return PiiStatus(enabled=True, usable=False, detail=f"{type(exc).__name__}: {exc}")
    if redactor is None:
        # Switched off is a valid, if loud, choice — not a failure to report as one.
        return PiiStatus(enabled=False, usable=True, detail="redaction is off")
    try:
        redactor.redact([_PROBE_TEXT])
    except PiiRedactionError as exc:
        return PiiStatus(enabled=True, usable=False, detail=str(exc))
    except Exception as exc:
        return PiiStatus(enabled=True, usable=False, detail=f"{type(exc).__name__}: {exc}")
    return PiiStatus(enabled=True, usable=True)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    statuses: tuple[BackendStatus, ...]
    pii: PiiStatus = PiiStatus(enabled=False, usable=True, detail="not checked")
    profile: ProfileChoice = NO_PROFILE
    suggest: Suggest | None = None

    @property
    def ok(self) -> bool:
        return all(status.usable for status in self.statuses) and self.pii.usable

    @property
    def unusable(self) -> tuple[BackendStatus, ...]:
        return tuple(status for status in self.statuses if not status.usable)

    def problem(self) -> str:
        """One sentence naming what to fix, or empty when nothing is wrong."""
        parts = [
            f"{status.role} → {status.kind}:{status.model} ({status.detail or 'unavailable'})"
            for status in self.unusable
        ]
        if not self.pii.usable:
            parts.append(f"redaction → {self.pii.detail or 'unavailable'}")
        return "; ".join(parts)


def suggest_for(statuses: tuple[BackendStatus, ...], key_set: bool) -> Suggest | None:
    """With a local role down: Hybrid if local triage still works, Cloud if not, and
    "add a key" when there is no key to move anything with."""
    if not any(not s.usable and s.kind in ("ollama", "vllm") for s in statuses):
        return None
    if not key_set:
        return "add_key"
    triage = next((s for s in statuses if s.role == "triage"), None)
    return "hybrid" if triage is not None and triage.usable else "cloud"


def diagnose(
    config: Config,
    prober: Prober = probe,
    *,
    check_pii: bool = False,
    secrets: SecretStore | None = None,
) -> DoctorReport:
    """Probe each role's backend, and optionally the redaction boundary.

    The PII probe is opt-in because it is the expensive one: it loads spaCy, which
    `PresidioDetector` otherwise defers to the first real `detect()`. The health endpoint
    and the pre-run check are called often and must stay cheap; `doctor` and `ui` run once
    and ask for it.

    Roles on the same backend and model are probed once: a profile that puts all five on
    one model costs one request, not five.
    """
    seen: dict[tuple[str, str | None, str], BackendStatus] = {}
    statuses: list[BackendStatus] = []
    for name in ROLES:
        role = config.role(name)
        key = (role.kind, role.base_url, role.model)
        if key not in seen:
            seen[key] = prober(role)
        status = replace(seen[key], role=name)
        if name in _TOOL_ROLES and name not in config.models.roles and status.tools is False:
            # The fallback to triage holds only while the triage model can do this role's
            # job; otherwise the role must be named.
            status = replace(
                status,
                model_present=False,
                detail=f"falls back to the triage model, which cannot call tools; "
                f"set models.roles.{name} or choose a model for it in Settings",
            )
        statuses.append(status)
    found = tuple(statuses)
    key_set = bool((secrets or default_secrets()).get(OPENROUTER))
    return DoctorReport(
        found,
        probe_pii(config) if check_pii else PiiStatus(False, True, "not checked"),
        config.profile,
        suggest_for(found, key_set),
    )
