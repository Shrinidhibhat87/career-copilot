"""Composition helpers: the recorder every client and agent reports to."""

from __future__ import annotations

from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.agents.recording import AgentRecording
from career_copilot.config import Config, ResolvedRole
from career_copilot.ports import Recorder


def recorder_for(config: Config, run_id: int | None = None) -> Recorder:
    """A recorder writing to the app database. It never raises into the caller."""
    return ObservabilityStore(config.db_path).recorder(run_id=run_id)


def agent_recording(
    config: Config, role: ResolvedRole, agent: str, run_id: int | None = None
) -> AgentRecording:
    return AgentRecording(
        recorder_for(config, run_id),
        agent=agent,
        model=role.litellm_model,
        backend=role.kind,
        run_id=run_id,
    )
