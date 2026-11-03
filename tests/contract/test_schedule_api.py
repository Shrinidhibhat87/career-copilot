"""The schedule over HTTP and in the store (spec 019). No network."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import schedule
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[TestClient, Config]]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    SqliteStore(config.db_path).close()
    app = FastAPI()
    app.include_router(schedule.router(lambda: config))
    yield TestClient(app), config


def test_nothing_saved_reads_as_off(world: tuple[TestClient, Config]) -> None:
    client, _ = world
    body = client.get("/api/schedule").json()
    assert body["enabled"] is False and body["times"] == [] and body["next"] is None
    assert body["lastRunAt"] is None and body["lastSkip"] is None
    # No CV in a fresh database, so a scheduled run would be skipped, and the page says why.
    assert body["canRun"] is False and body["whyNot"]


def test_saving_returns_sorted_times_and_the_next_slot(world: tuple[TestClient, Config]) -> None:
    client, _ = world
    body = client.put("/api/schedule", json={"enabled": True, "times": ["18:00", "08:00"]}).json()
    assert body["enabled"] is True and body["times"] == ["08:00", "18:00"]
    assert body["next"] is not None


def test_versions_round_trip_and_the_newest_wins(world: tuple[TestClient, Config]) -> None:
    client, config = world
    client.put("/api/schedule", json={"enabled": True, "times": ["08:00"]})
    client.put("/api/schedule", json={"enabled": False, "times": ["09:00"]})
    with SqliteStore(config.db_path) as store:
        saved = store.schedule_setting()
    assert saved is not None and saved.version == 2 and saved.enabled is False
    assert saved.times == ("09:00",)


@pytest.mark.parametrize(
    "body",
    [
        {"enabled": "yes", "times": ["08:00"]},
        {"enabled": True, "times": []},
        {"enabled": True, "times": ["8am"]},
        {"enabled": True, "times": ["01:00", "02:00", "03:00", "04:00", "05:00"]},
        {"enabled": True},
    ],
)
def test_bad_bodies_are_refused_and_nothing_is_saved(
    world: tuple[TestClient, Config], body: dict[str, object]
) -> None:
    client, config = world
    assert client.put("/api/schedule", json=body).status_code == 422
    with SqliteStore(config.db_path) as store:
        assert store.schedule_setting() is None


def test_runs_default_to_manual_and_a_scheduled_one_is_remembered(
    world: tuple[TestClient, Config],
) -> None:
    _, config = world
    with SqliteStore(config.db_path) as store:
        store._conn.execute(
            "INSERT INTO runs (started_at, kind, state) VALUES (?, 'full', 'succeeded')",
            (datetime(2030, 1, 1, tzinfo=UTC).isoformat(),),
        )
        store._conn.commit()
        row = store._conn.execute('SELECT "trigger" FROM runs').fetchone()
        assert row[0] == "manual"
        assert store.last_run_started_at("scheduled") is None
        assert store.last_run_started_at() is not None
