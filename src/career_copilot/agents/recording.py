"""Recording for pydantic-ai agent runs: one row per run, one per tool call.

Agents bypass `LiteLLMClient`, so they report here instead. Only numbers, names and
outcomes leave this module; tool arguments and replies never do. A recorder failure is
logged and dropped, never raised into the run.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from career_copilot.ports import LlmCallRecord, Recorder, ToolCallRecord

log = logging.getLogger(__name__)


def tool_outcome(reply: object) -> str:
    """Read a tool's outcome from its reply: the tools signal refusals in the first words."""
    text = str(reply)
    if text.startswith("refused"):
        return "refused"
    if text.startswith("search failed"):
        return "error"
    return "ok"


class AgentRecording:
    """Identity of one agent run plus the clock that separates tool time from model time."""

    def __init__(
        self,
        recorder: Recorder,
        agent: str,
        model: str,
        backend: str,
        profile: str | None = None,
        run_id: int | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._recorder = recorder
        self.agent = agent
        self.model = model
        self.backend = backend
        self.profile = profile
        self.run_id = run_id
        self._clock = clock
        self._started = 0.0
        self._tool_seconds = 0.0
        self._agent_run = uuid.uuid4().hex

    def start(self) -> None:
        self._started = self._clock()
        self._tool_seconds = 0.0
        self._agent_run = uuid.uuid4().hex

    def timed_tool(self, tool: str, fn: Callable[..., str]) -> Callable[..., str]:
        """Wrap a tool body: time it, record its outcome, pass its reply through."""

        def wrapper(*args: Any, **kwargs: Any) -> str:
            began = self._clock()
            outcome = "ok"
            try:
                reply = fn(*args, **kwargs)
                outcome = tool_outcome(reply)
                return reply
            except Exception:
                outcome = "error"
                raise
            finally:
                seconds = self._clock() - began
                self._tool_seconds += seconds
                self._safely(
                    lambda: self._recorder.tool_call(
                        ToolCallRecord(
                            self.agent, tool, seconds, outcome, self.run_id, self._agent_run
                        )
                    )
                )

        return wrapper

    def finish(self, result: Any | None, outcome: str = "ok") -> None:
        """Write the run's row. `result` is None when the run ended without one."""
        elapsed = max(self._clock() - self._started - self._tool_seconds, 0.0)
        usage = None
        if result is not None:
            raw = result.usage  # a property in pydantic-ai 2.x, a method before
            usage = raw() if callable(raw) else raw
        self._safely(
            lambda: self._recorder.llm_call(
                LlmCallRecord(
                    stage=self.agent,
                    model=self.model,
                    backend=self.backend,
                    profile=self.profile,
                    run_id=self.run_id,
                    kind="agent_run",
                    requests=usage.requests if usage else 1,
                    tokens_in=usage.input_tokens if usage else None,
                    tokens_out=usage.output_tokens if usage else None,
                    latency_s=elapsed,
                    outcome=outcome,
                    agent_run=self._agent_run,
                    cost_eur=self._price(result),
                )
            )
        )

    @staticmethod
    def _price(result: Any | None) -> float:
        """What the run cost when the framework can price it; 0 when it cannot."""
        if result is None:
            return 0.0
        try:
            total = 0.0
            for message in result.all_messages():
                cost = getattr(message, "cost", None)
                if callable(cost):
                    total += float(cost().total_price)
            return total
        except Exception:
            return 0.0

    @staticmethod
    def _safely(write: Callable[[], None]) -> None:
        try:
            write()
        except Exception as exc:
            log.warning("recording an agent call failed: %s", type(exc).__name__)


def timed(
    recording: AgentRecording | None, tool: str, fn: Callable[..., str]
) -> Callable[..., str]:
    """`timed_tool` when a recording exists, the bare function when it does not."""
    return fn if recording is None else recording.timed_tool(tool, fn)
