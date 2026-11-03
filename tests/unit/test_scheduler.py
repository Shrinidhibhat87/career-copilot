"""The ticker with a fake clock and a fake start (spec 019). No threads, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from career_copilot.adapters.scheduler import RETRY_AFTER, Scheduler
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.schedule import slots_from_times


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.config = Config(db_path=str(tmp_path / "jobs.db"))
        SqliteStore(self.config.db_path).close()
        self.clock = datetime(2030, 1, 10, 7, 0).astimezone()
        self.started: list[str] = []
        self.busy = False
        self.answer: dict[str, Any] = {"started": True}
        self.scheduler = Scheduler(self.config, self.start, lambda: self.busy, lambda: self.clock)

    def start(self, config: Config, *, trigger: str, queue: bool) -> dict[str, Any]:
        assert queue is False
        self.started.append(trigger)
        if self.answer["started"]:
            with SqliteStore(config.db_path) as store:
                store._conn.execute(
                    'INSERT INTO runs (started_at, kind, state, "trigger") VALUES (?,?,?,?)',
                    (self.clock.astimezone(UTC).isoformat(), "full", "succeeded", trigger),
                )
                store._conn.commit()
        return self.answer

    def schedule(self, enabled: bool = True, times: tuple[str, ...] = ("08:00",)) -> None:
        with SqliteStore(self.config.db_path) as store:
            store.save_schedule(enabled, slots_from_times(times), self.clock)

    def at(self, hour: int, day: int = 10) -> None:
        self.clock = self.clock.replace(day=day, hour=hour, minute=0)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_nothing_runs_without_a_schedule_or_when_it_is_off(world: World) -> None:
    world.at(9)
    world.scheduler.tick()
    world.schedule(enabled=False)
    world.scheduler.tick()
    assert world.started == []


def test_a_due_slot_starts_one_scheduled_run_and_not_a_second(world: World) -> None:
    world.schedule()
    world.at(7)
    world.scheduler.tick()
    assert world.started == []
    world.at(9)
    world.scheduler.tick()
    world.scheduler.tick()
    assert world.started == ["scheduled"]


def test_a_failed_run_is_not_retried_within_its_slot(world: World) -> None:
    world.schedule()
    world.at(9)
    world.scheduler.tick()
    with SqliteStore(world.config.db_path) as store:
        store._conn.execute("UPDATE runs SET state = 'failed'")
        store._conn.commit()
    world.at(10)
    world.scheduler.tick()
    assert world.started == ["scheduled"]


def test_three_missed_days_catch_up_with_one_run(world: World) -> None:
    world.schedule()
    world.at(9)
    world.scheduler.tick()
    world.at(9, day=13)
    world.scheduler.tick()
    world.scheduler.tick()
    assert world.started == ["scheduled", "scheduled"]


def test_an_active_run_is_skipped_without_a_recorded_skip(world: World) -> None:
    world.schedule()
    world.at(9)
    world.busy = True
    world.scheduler.tick()
    assert world.started == [] and world.scheduler.last_skip is None
    world.busy = False
    world.scheduler.tick()
    assert world.started == ["scheduled"]


def test_a_skip_is_remembered_and_retried_only_after_fifteen_minutes(world: World) -> None:
    world.schedule()
    world.at(9)
    world.answer = {"started": False, "reason": "scheduled runs only use local models"}
    world.scheduler.tick()
    assert world.scheduler.last_skip is not None
    assert world.scheduler.last_skip[0] == "scheduled runs only use local models"
    world.clock += RETRY_AFTER - timedelta(minutes=1)
    world.scheduler.tick()
    assert len(world.started) == 1
    world.answer = {"started": True}
    world.clock += timedelta(minutes=2)
    world.scheduler.tick()
    assert len(world.started) == 2 and world.scheduler.last_skip is None


def test_a_broken_start_does_not_kill_the_ticker(world: World) -> None:
    world.schedule()
    world.at(9)

    def boom(config: Config, *, trigger: str, queue: bool) -> dict[str, Any]:
        raise RuntimeError("no")

    world.scheduler._start = boom
    world.scheduler.tick()


def test_a_slot_from_before_the_schedule_was_saved_is_not_owed(world: World) -> None:
    world.at(9)
    world.schedule()  # saved at 09:00, after today's 08:00 slot
    world.scheduler.tick()
    assert world.started == []


def test_a_slot_due_while_a_run_is_active_starts_once_that_run_ends(world: World) -> None:
    """Opening at 12:55 with a 13:00 slot: the slot is not lost to the active run."""
    world.schedule(times=("13:00",))
    world.at(13)
    world.busy = True
    world.scheduler.tick()
    world.scheduler.tick()
    assert world.started == []
    world.busy = False
    world.scheduler.tick()
    assert world.started == ["scheduled"]
