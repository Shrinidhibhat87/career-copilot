from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from career_copilot.domain.budget import BudgetExceeded, CostMeter, RunLimits
from career_copilot.usecases.score import resolve_budget


def test_empty_store_gets_the_initial_target() -> None:
    budget = resolve_budget(False, initial_target=50, incremental_target=6, max_eur=1.0)
    assert (budget.target, budget.mode) == (50, "initial")


def test_a_store_with_match_scores_gets_the_incremental_target() -> None:
    budget = resolve_budget(True, initial_target=50, incremental_target=6, max_eur=1.0)
    assert (budget.target, budget.mode) == (6, "incremental")


def test_force_initial_overrides_detection() -> None:
    budget = resolve_budget(
        True, initial_target=50, incremental_target=6, max_eur=1.0, force_initial=True
    )
    assert (budget.target, budget.mode) == (50, "initial")


def test_an_explicit_override_wins_over_everything() -> None:
    budget = resolve_budget(
        False, initial_target=50, incremental_target=6, max_eur=1.0, force_initial=True, override=3
    )
    assert (budget.target, budget.mode) == (3, "override")


def test_cost_meter_accumulates() -> None:
    meter = CostMeter()
    meter.add(0.01)
    meter.add(0.02)
    assert meter.calls == 2
    assert meter.spent == pytest.approx(0.03)


def test_cost_meter_raises_once_the_ceiling_is_passed() -> None:
    meter = CostMeter()
    meter.add(0.6)
    meter.check(1.0)
    meter.add(0.5)
    with pytest.raises(BudgetExceeded, match=r"ceiling €1\.0"):
        meter.check(1.0)


def test_a_zero_ceiling_means_no_ceiling() -> None:
    meter = CostMeter()
    meter.add(99.0)
    meter.check(0.0)


def test_cost_meter_refuses_a_call_that_could_cross_the_ceiling() -> None:
    meter = CostMeter()
    meter.add(0.08)
    with pytest.raises(BudgetExceeded, match="next call reserves"):
        meter.reserve(0.10, 0.03)


def test_cost_meter_allows_a_call_that_fits_exactly() -> None:
    meter = CostMeter()
    meter.add(0.08)
    meter.reserve(0.10, 0.02)


def test_steps_are_counted_and_capped() -> None:
    meter = CostMeter(RunLimits(max_steps=2))
    started = datetime(2026, 9, 8, tzinfo=UTC)
    meter.step(started, started)
    meter.step(started, started)
    with pytest.raises(BudgetExceeded, match="took 2 steps"):
        meter.step(started, started)


def test_a_deadline_stops_a_run_that_costs_nothing() -> None:
    meter = CostMeter(RunLimits(max_seconds=30))
    started = datetime(2026, 9, 8, tzinfo=UTC)
    meter.step(started, started)
    with pytest.raises(BudgetExceeded, match="ceiling 30s"):
        meter.step(started + timedelta(seconds=31), started)


def test_without_limits_a_free_run_never_stops_itself() -> None:
    meter = CostMeter()
    started = datetime(2026, 9, 8, tzinfo=UTC)
    for _ in range(500):
        meter.step(started + timedelta(hours=9), started)
    meter.check(0.0)
    assert meter.steps == 500
