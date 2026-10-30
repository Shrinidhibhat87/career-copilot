"""`doctor`'s lines about job sources, from the stored status. No network, no real database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _pytest.capture import CaptureFixture

import career_copilot.__main__ as cli
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config, JobsucheSpec, TargetSpec
from career_copilot.domain.source_health import FailureReason, HealthConfig, record

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


class Board:
    def __init__(self, name: str) -> None:
        self.name = name

    def fetch(self, target: Any) -> list[Any]:
        raise AssertionError("doctor must never fetch a board")


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(
        cli, "build_sources", lambda: {n: Board(n) for n in ("greenhouse", "lever", "workable")}
    )
    monkeypatch.setattr(cli, "SEARCH_SOURCES", {})
    cfg = Config(
        db_path=str(tmp_path / "jobs.db"),
        jobsuche=JobsucheSpec(enabled=False),
        targets=[
            TargetSpec(source="greenhouse", identifier="a"),
            TargetSpec(source="greenhouse", identifier="b"),
            TargetSpec(source="lever", identifier="c"),
            TargetSpec(source="workable", identifier="d"),
        ],
    )
    with SqliteStore(cfg.db_path):
        pass
    return cfg


def fail(store: SqliteStore, source: str, times: int, reason: FailureReason) -> None:
    health = None
    for _ in range(times):
        health = record(health, source, False, NOW, HealthConfig(), reason=reason)
    assert health is not None
    store.save_source_health(health)


def lines(capsys: CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_each_source_gets_one_line_from_its_stored_status(
    config: Config, capsys: CaptureFixture[str]
) -> None:
    with SqliteStore(config.db_path) as store:
        run = store.start_run(NOW)
        store.record_source_run(run, "greenhouse", "a", True, 40, None, NOW - timedelta(days=1))
        store.save_source_health(record(None, "greenhouse", True, NOW, HealthConfig()))
        fail(store, "workable", 3, FailureReason.RATE_LIMITED)
        fail(store, "lever", 1, FailureReason.TIMEOUT)
    cli._report_sources(config)
    assert lines(capsys) == [
        "warn source workable — paused until 2026-08-13 09:00 UTC: asked us to slow down and is "
        "being left alone for a while (1 board)",
        "warn source lever — having trouble: took too long to answer (1 board)",
        "ok   source greenhouse — working, 40 jobs in the last 7 days (2 boards)",
    ]


def test_a_source_never_tried_says_so(config: Config, capsys: CaptureFixture[str]) -> None:
    cli._report_sources(config)
    assert "--   source lever — not tried yet (1 board)" in lines(capsys)


def test_doctor_never_fetches_a_board(config: Config, capsys: CaptureFixture[str]) -> None:
    # `Board.fetch` raises: reaching it would fail this test.
    cli._report_sources(config)
    assert lines(capsys)


def test_a_searched_source_that_works_is_reported_by_its_live_check_only(
    config: Config, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    class Searched:
        def ping(self) -> int:
            return 7

    monkeypatch.setattr(cli, "SEARCH_SOURCES", {"jobsuche": Searched})
    monkeypatch.setattr(cli, "build_sources", lambda: {"jobsuche": Board("jobsuche")})
    cfg = config.model_copy(update={"jobsuche": JobsucheSpec(enabled=True)})
    cfg.targets.clear()
    with SqliteStore(cfg.db_path) as store:
        store.save_source_health(record(None, "jobsuche", True, NOW, HealthConfig()))
    cli._report_sources(cfg)
    out = lines(capsys)
    assert not any("working," in line for line in out)
    assert any(line.startswith("ok   source jobsuche — reachable (7 postings") for line in out)
