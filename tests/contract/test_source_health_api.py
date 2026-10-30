"""Source health over HTTP and the `sources test` / `sources resume` commands. No network."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import source_health
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config, JobsucheSpec, TargetSpec
from career_copilot.domain.models import RawJob, SourceTarget
from career_copilot.domain.source_health import FailureReason, HealthConfig, SourceHealth, record
from tests.conftest import make_raw

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


class Board:
    def __init__(self, name: str, jobs: int = 2, error: Exception | None = None) -> None:
        self.name, self.jobs, self.error, self.calls = name, jobs, error, 0

    def fetch(self, target: SourceTarget) -> list[RawJob]:
        self.calls += 1
        if self.error:
            raise self.error
        return [make_raw(source_job_id=str(i)) for i in range(self.jobs)]


class Searched:
    name = "jobsuche"

    def __init__(self, total: int = 1234) -> None:
        self.total, self.pings = total, 0

    def fetch(self, target: SourceTarget) -> list[RawJob]:
        raise AssertionError("a test must use the single-request ping")

    def ping(self) -> int:
        self.pings += 1
        return self.total


class Ticker:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict[str, Any]]:
    config = Config(
        db_path=str(tmp_path / "jobs.db"),
        targets=[
            TargetSpec(source="greenhouse", identifier="acme"),
            TargetSpec(source="lever", identifier="beta"),
        ],
        jobsuche=JobsucheSpec(enabled=False),
    )
    adapters: dict[str, Any] = {
        "greenhouse": Board("greenhouse"),
        "lever": Board("lever"),
        "workable": Board("workable"),
    }
    ticker = Ticker()
    app = FastAPI()
    app.include_router(
        source_health.router(lambda: config, lambda: adapters, clock=lambda: NOW, monotonic=ticker)
    )
    with SqliteStore(config.db_path):
        pass
    yield {"client": TestClient(app), "config": config, "adapters": adapters, "ticker": ticker}


def pause(config: Config, source: str, reason: FailureReason = FailureReason.FORBIDDEN) -> None:
    cfg = HealthConfig()
    health: SourceHealth | None = None
    for _ in range(3):
        health = record(health, source, False, NOW, cfg, reason=reason, message="x https://a/?k=1")
    assert health is not None
    with SqliteStore(config.db_path) as store:
        store.save_source_health(health)


def snapshot(config: Config) -> list[int]:
    with SqliteStore(config.db_path) as store:
        return [
            store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("jobs", "cooldowns", "source_health", "source_runs")
        ]


def test_health_lists_sources_with_a_board_and_hides_idle_ones(world: dict[str, Any]) -> None:
    rows = world["client"].get("/api/sources/health").json()
    assert [(r["source"], r["status"], r["targets"]) for r in rows] == [
        ("greenhouse", "unknown", 1),
        ("lever", "unknown", 1),
    ]


def test_a_paused_source_comes_first_with_its_reason_in_words(world: dict[str, Any]) -> None:
    pause(world["config"], "lever")
    rows = world["client"].get("/api/sources/health").json()
    first = rows[0]
    assert first["source"] == "lever" and first["status"] == "paused"
    assert first["lastFailure"] == "forbidden" and first["lastMessage"] == "refused access"
    assert first["pausedUntil"] == (NOW + timedelta(hours=24)).isoformat()
    assert "https://" not in str(first)


def test_a_source_with_history_but_no_board_is_still_listed(world: dict[str, Any]) -> None:
    pause(world["config"], "workable")
    sources = [r["source"] for r in world["client"].get("/api/sources/health").json()]
    assert "workable" in sources


def test_fetched_last_seven_days_counts_only_recent_successes(world: dict[str, Any]) -> None:
    with SqliteStore(world["config"].db_path) as store:
        run = store.start_run(NOW)
        store.record_source_run(run, "greenhouse", "acme", True, 5, None, NOW - timedelta(days=1))
        store.record_source_run(run, "greenhouse", "acme", True, 7, None, NOW - timedelta(days=9))
        store.record_source_run(run, "greenhouse", "acme", False, 0, "x", NOW - timedelta(days=1))
    rows = {r["source"]: r for r in world["client"].get("/api/sources/health").json()}
    assert rows["greenhouse"]["fetchedLast7Days"] == 5


def test_a_test_reports_count_and_time_and_writes_nothing(world: dict[str, Any]) -> None:
    before = snapshot(world["config"])
    body = world["client"].post("/api/sources/greenhouse/test").json()
    assert body["ok"] is True and body["fetched"] == 2 and body["reason"] is None
    assert isinstance(body["durationMs"], int)
    assert snapshot(world["config"]) == before


def test_a_failed_test_gives_the_reason_in_words_not_the_raw_error(world: dict[str, Any]) -> None:
    world["adapters"]["greenhouse"] = Board(
        "greenhouse", error=httpx.ReadTimeout("slow https://boards-api.x/?secret=1")
    )
    before = snapshot(world["config"])
    body = world["client"].post("/api/sources/greenhouse/test").json()
    assert body["ok"] is False and body["reason"] == "timeout"
    assert body["message"] == "took too long to answer" and "secret" not in str(body)
    assert snapshot(world["config"]) == before


def test_a_test_while_paused_is_allowed_and_leaves_the_pause_alone(world: dict[str, Any]) -> None:
    pause(world["config"], "lever")
    body = world["client"].post("/api/sources/lever/test").json()
    assert body["ok"] is True
    rows = {r["source"]: r for r in world["client"].get("/api/sources/health").json()}
    assert rows["lever"]["status"] == "paused"


def test_a_source_is_tested_at_most_once_a_minute(world: dict[str, Any]) -> None:
    client, ticker = world["client"], world["ticker"]
    assert client.post("/api/sources/greenhouse/test").status_code == 200
    again = client.post("/api/sources/greenhouse/test")
    assert again.status_code == 429 and int(again.headers["retry-after"]) >= 1
    assert world["adapters"]["greenhouse"].calls == 1
    assert client.post("/api/sources/lever/test").status_code == 200  # another source is fine
    ticker.t += 61
    assert client.post("/api/sources/greenhouse/test").status_code == 200


def test_a_source_with_no_board_says_so(world: dict[str, Any]) -> None:
    body = world["client"].post("/api/sources/workable/test").json()
    assert body["ok"] is False and "no board" in body["message"]


def test_a_searched_source_is_tested_with_one_ping(world: dict[str, Any], tmp_path: Path) -> None:
    config = Config(db_path=str(tmp_path / "s.db"))
    searched = Searched()
    app = FastAPI()
    app.include_router(
        source_health.router(lambda: config, lambda: {"jobsuche": searched}, clock=lambda: NOW)
    )
    with SqliteStore(config.db_path):
        pass
    body = TestClient(app).post("/api/sources/jobsuche/test").json()
    assert body["ok"] is True and body["fetched"] == 1234 and searched.pings == 1


def test_an_unknown_source_is_404_for_test_and_resume(world: dict[str, Any]) -> None:
    client = world["client"]
    assert client.post("/api/sources/nope/test").status_code == 404
    assert client.post("/api/sources/nope/resume").status_code == 404


def test_resume_cuts_a_pause_short_and_changes_nothing_else(world: dict[str, Any]) -> None:
    pause(world["config"], "lever")
    body = world["client"].post("/api/sources/lever/resume").json()
    assert body == {"status": "degraded"}
    with SqliteStore(world["config"].db_path) as store:
        health = store.source_health("lever")
    assert health is not None and health.paused_until == NOW
    assert health.consecutive_failures == 3 and health.last_failure is FailureReason.FORBIDDEN


def test_resume_on_a_source_that_is_not_paused_is_a_no_op(world: dict[str, Any]) -> None:
    assert world["client"].post("/api/sources/greenhouse/resume").json() == {"status": "unknown"}
    with SqliteStore(world["config"].db_path) as store:
        assert store.source_health("greenhouse") is None
