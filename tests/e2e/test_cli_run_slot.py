"""One run slot, shared by the terminal and the browser — and what does not need it."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from career_copilot.__main__ import app
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.runs import RunConfig
from tests.accounts_helper import make_account

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def workspace(tmp_path: Path) -> tuple[str, str]:
    db = make_account(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(f"db_path: {tmp_path / 'jobs.db'}\ntargets: []\n")
    return str(config), str(db)


def hold_the_slot(db: str) -> None:
    frozen = RunConfig(Preferences(), "triage", "match", 1, "initial", 1.0, 1, 0.02, 0.2, 256, 1024)
    with SqliteStore(db) as store:
        store.begin_run("full", frozen, NOW, os.getpid(), "birth")


def test_a_held_slot_is_reported_not_raised(tmp_path: Path) -> None:
    config, db = workspace(tmp_path)
    hold_the_slot(db)
    result = CliRunner().invoke(app, ["discover", "--config", config])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "a run is already active" in result.output


def test_digest_reads_without_taking_the_slot(tmp_path: Path) -> None:
    config, db = workspace(tmp_path)
    hold_the_slot(db)
    out = tmp_path / "digest.md"
    result = CliRunner().invoke(app, ["digest", "--config", config, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    with SqliteStore(db) as store:
        assert [row["kind"] for row in store.list_runs()] == ["full"]
