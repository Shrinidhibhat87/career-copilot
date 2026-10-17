"""What one run may consume: money, calls, and time.

Money alone stopped being enough when a role could be served locally. A local call
costs nothing, so a euro ceiling never binds and an agent loop against Ollama would
run until the process was killed. `RunLimits` bounds the same run by steps and by
wall clock, and `CostMeter` checks all three in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


class BudgetExceeded(RuntimeError):
    """A run limit was passed. A partial run is fine; a silent overrun is not."""


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Non-monetary ceilings. Zero means no ceiling, as it does for euros."""

    max_steps: int = 0
    max_seconds: float = 0.0

    def deadline(self, started_at: datetime) -> datetime | None:
        if self.max_seconds <= 0:
            return None
        return started_at + timedelta(seconds=self.max_seconds)


class CostMeter:
    """Running spend for one run, with a reservation before every model call."""

    def __init__(self, limits: RunLimits | None = None) -> None:
        self.spent = 0.0
        self.calls = 0
        self.steps = 0
        self._limits = limits or RunLimits()

    def add(self, eur: float) -> None:
        self.spent += eur
        self.calls += 1

    def add_usage(self, calls: int, eur: float) -> None:
        self.spent += eur
        self.calls += calls

    def check(self, limit: float) -> None:
        """A limit of 0 means no ceiling."""
        if limit > 0 and self.spent > limit:
            raise BudgetExceeded(f"spent €{self.spent:.4f}, ceiling €{limit}")

    def reserve(self, limit: float, worst_case_eur: float) -> None:
        """Refuse a call unless its configured worst case fits the remaining budget."""
        if limit > 0 and self.spent + worst_case_eur > limit:
            remaining = max(0.0, limit - self.spent)
            raise BudgetExceeded(
                f"next call reserves €{worst_case_eur:.4f}, "
                f"but only €{remaining:.4f} remains of the €{limit:.4f} ceiling"
            )

    def step(self, now: datetime, started_at: datetime) -> None:
        """Count one loop iteration and refuse to start another past a ceiling.

        Called before the work, so the step that would exceed a limit is the one that
        never happens: an agent stops with the results it has rather than mid-tool-call.
        """
        limits = self._limits
        deadline = limits.deadline(started_at)
        if deadline is not None and now >= deadline:
            elapsed = (now - started_at).total_seconds()
            raise BudgetExceeded(f"ran for {elapsed:.0f}s, ceiling {limits.max_seconds:.0f}s")
        if limits.max_steps > 0 and self.steps >= limits.max_steps:
            raise BudgetExceeded(f"took {self.steps} steps, ceiling {limits.max_steps}")
        self.steps += 1
