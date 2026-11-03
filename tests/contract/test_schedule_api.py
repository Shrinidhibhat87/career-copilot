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


def slots(*times: str, on: bool = True) -> list[dict[str, object]]:
    return [{"time": t, "on": on} for t in times]


FOUR = slots("08:00", "12:30", "18:00", "22:00")


def test_nothing_saved_reads_as_off_with_the_default_slots(
    world: tuple[TestClient, Config],
) -> None:
    client, _ = world
    body = client.get("/api/schedule").json()
    assert body["enabled"] is False and body["times"] == [] and body["next"] is None
    assert [s["time"] for s in body["slots"]] == ["08:00", "12:30", "18:00", "22:00"]
    assert [s["on"] for s in body["slots"]] == [True, False, True, False]
    assert body["lastRunAt"] is None and body["lastSkip"] is None
    # No CV in a fresh database, so a scheduled run would be skipped, and the page says why.
    assert body["canRun"] is False and body["whyNot"]


def test_saving_returns_the_slots_the_on_times_and_the_next_slot(
    world: tuple[TestClient, Config],
) -> None:
    client, _ = world
    saved = [
        {"time": "18:00", "on": True},
        {"time": "12:30", "on": False},
        *slots("08:00", "22:00"),
    ]
    body = client.put("/api/schedule", json={"enabled": True, "slots": saved}).json()
    assert body["enabled"] is True and body["times"] == ["08:00", "18:00", "22:00"]
    assert body["slots"][1] == {"time": "12:30", "on": False}
    assert body["next"] is not None


def test_versions_round_trip_and_the_newest_wins(world: tuple[TestClient, Config]) -> None:
    client, config = world
    client.put("/api/schedule", json={"enabled": True, "slots": FOUR})
    client.put(
        "/api/schedule", json={"enabled": False, "slots": slots("09:00", "10:00", "11:00", "12:00")}
    )
    with SqliteStore(config.db_path) as store:
        saved = store.schedule_setting()
    assert saved is not None and saved.version == 2 and saved.enabled is False
    assert saved.times == ("09:00", "10:00", "11:00", "12:00")


def test_a_row_saved_before_slots_existed_still_reads(world: tuple[TestClient, Config]) -> None:
    client, config = world
    with SqliteStore(config.db_path) as store:
        store._conn.execute(
            "INSERT INTO schedule_settings (enabled, times, saved_at) VALUES (1, ?, ?)",
            ('["07:00", "19:00"]', datetime(2030, 1, 1, tzinfo=UTC).isoformat()),
        )
        store._conn.commit()
    body = client.get("/api/schedule").json()
    assert body["times"] == ["07:00", "19:00"]
    assert [s["on"] for s in body["slots"]] == [True, True, False, False]


@pytest.mark.parametrize(
    "body",
    [
        {"enabled": "yes", "slots": FOUR},
        {"enabled": True, "slots": []},
        {"enabled": True, "slots": slots("08:00", "09:00", "10:00", "10:07")},
        {"enabled": True, "slots": slots("08:00", "08:00", "10:00", "11:00")},
        {"enabled": True, "slots": slots("08:00", "09:00", "10:00", "11:00", "12:00")},
        {"enabled": True, "slots": slots("08:00", "09:00", "10:00", "11:00", on=False)},
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


def test_switching_the_schedule_off_needs_no_slot_on(world: tuple[TestClient, Config]) -> None:
    client, _ = world
    off = slots("08:00", "09:00", "10:00", "11:00", on=False)
    assert client.put("/api/schedule", json={"enabled": False, "slots": off}).status_code == 200


def test_the_open_time_refresh_stands_down_when_a_schedule_is_on(
    world: tuple[TestClient, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    from career_copilot.adapters.api import runs

    client, config = world
    monkeypatch.setattr(runs, "REFRESH_ROLES", ())  # no billed role to answer first
    client.put("/api/schedule", json={"enabled": True, "slots": FOUR})
    assert runs.request_refresh(config, if_stale=True)["reason"] == "the schedule decides"
    assert runs.request_refresh(config)["reason"] == "upload a CV first"  # a click still works


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
