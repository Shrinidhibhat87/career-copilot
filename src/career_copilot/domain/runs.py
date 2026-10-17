"""Immutable run inputs and progress shared by the ledger and pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from career_copilot.domain.preferences import Preferences

REQUEST_TIMEOUT = 120
HEARTBEAT_TIMEOUT = 300


class RunInProgress(RuntimeError):
    """The database already has a live owner of the run slot."""


class RunCancelled(RuntimeError):
    """Cooperative shutdown, checked between jobs."""


@dataclass(frozen=True, slots=True)
class Progress:
    stage: str
    completed: int
    total: int
    cost_eur: float
    budget_eur: float
    failed: int = 0


@dataclass(frozen=True, slots=True)
class RunConfig:
    preferences: Preferences
    triage_model: str
    match_model: str
    rubric_version: int
    mode: str
    budget_eur: float
    target: int
    triage_reserve_eur: float
    match_reserve_eur: float
    triage_max_output_tokens: int
    match_max_output_tokens: int

    @property
    def preferences_version(self) -> int:
        return self.preferences.version

    @property
    def preferences_hash(self) -> str:
        return self.preferences.prompt_hash()

    def payload(self) -> dict[str, object]:
        result = asdict(self)
        result["preferences"] = self.preferences.to_payload()
        result["preferences_version"] = self.preferences_version
        return result

    def token(self) -> str:
        # Include reserves/targets as well: changing those changes what is approved.
        return hashlib.sha256(json.dumps(self.payload(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ScorePreview:
    candidates_now: int
    estimated_calls: int
    reserved_eur: float
    ceiling_eur: float
    mode: str
    token: str


def stalled(heartbeat: str | None, now: datetime) -> bool:
    return (
        heartbeat is None
        or (now - datetime.fromisoformat(heartbeat)).total_seconds() > HEARTBEAT_TIMEOUT
    )
