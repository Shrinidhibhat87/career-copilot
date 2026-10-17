"""An app start opens a log and a session row; the end closes both. A crash is `unclean`."""

from __future__ import annotations

import gzip
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from career_copilot.adapters.observability_session import AppSession
from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.config import ObservabilitySpec
from career_copilot.ports import LlmCallRecord

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> ObservabilityStore:
    return ObservabilityStore(tmp_path / "jobs.db")


@pytest.fixture(autouse=True)
def clean_root_logger() -> object:
    root = logging.getLogger()
    before = list(root.handlers), root.level
    yield
    root.handlers[:] = before[0]
    root.setLevel(before[1])


def session(
    store: ObservabilityStore, tmp_path: Path, now: datetime = NOW, **spec: object
) -> AppSession:
    return AppSession(
        store,
        ObservabilitySpec.model_validate(spec),
        tmp_path / "logs",
        clock=lambda: now,
        profile="laptop",
    )


def lines(path: str | Path) -> list[str]:
    return Path(path).read_text().splitlines()


def test_start_and_end_write_the_first_and_last_log_lines(
    store: ObservabilityStore, tmp_path: Path
) -> None:
    s = session(store, tmp_path)
    s.start()
    logging.getLogger("career_copilot.x").info("something happened")
    s.end()
    text = lines(s.log_path)
    assert "session start" in text[0]
    assert "something happened" in text[1]
    assert "session end" in text[-1] and "0s" in text[-1]
    (row,) = store.sessions()
    assert (row["outcome"], row["profile"], row["logPath"]) == ("clean", "laptop", str(s.log_path))


def test_an_open_session_at_start_becomes_unclean_at_its_logs_last_write(
    store: ObservabilityStore, tmp_path: Path
) -> None:
    crashed = session(store, tmp_path, NOW - timedelta(days=1))
    crashed.start()
    last_write = NOW - timedelta(hours=20)
    os.utime(crashed.log_path, (last_write.timestamp(), last_write.timestamp()))
    logging.getLogger().removeHandler(logging.getLogger().handlers[-1])  # the crash

    session(store, tmp_path).start()

    old, new = store.sessions()[1], store.sessions()[0]
    assert old["outcome"] == "unclean"
    assert datetime.fromisoformat(old["endedAt"]) == last_write
    assert new["outcome"] is None


def test_a_missing_log_falls_back_to_the_start_time(
    store: ObservabilityStore, tmp_path: Path
) -> None:
    started = NOW - timedelta(hours=5)
    store.open_session(started, None, str(tmp_path / "logs" / "gone.log"))
    session(store, tmp_path).start()
    old = store.sessions()[1]
    assert (old["outcome"], datetime.fromisoformat(old["endedAt"])) == ("unclean", started)


def test_logs_older_than_log_days_are_deleted_at_start(
    store: ObservabilityStore, tmp_path: Path
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    old, fresh, other = logs / "app-old.log", logs / "app-new.log", logs / "keep.txt"
    for f in (old, fresh, other):
        f.write_text("x")
    stale = (NOW - timedelta(days=15)).timestamp()
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    os.utime(fresh, ((NOW - timedelta(days=13)).timestamp(),) * 2)
    session(store, tmp_path, log_days=14).start()
    assert not old.exists()
    assert fresh.exists() and other.exists()


def test_archive_logs_gzips_the_log_into_the_session_row(
    store: ObservabilityStore, tmp_path: Path
) -> None:
    s = session(store, tmp_path, archive_logs=True)
    s.start()
    s.end()
    blob = sqlite3.connect(store.path).execute("SELECT log_gz FROM app_sessions").fetchone()[0]
    assert "session start" in gzip.decompress(blob).decode()


def test_the_log_is_not_archived_by_default(store: ObservabilityStore, tmp_path: Path) -> None:
    s = session(store, tmp_path)
    s.start()
    s.end()
    assert (
        sqlite3.connect(store.path).execute("SELECT log_gz FROM app_sessions").fetchone()[0] is None
    )


def test_start_rolls_up_rows_older_than_detail_days(
    store: ObservabilityStore, tmp_path: Path
) -> None:
    call = LlmCallRecord("match", "m", "ollama", "request", 2.0, "ok", tokens_in=1, tokens_out=2)
    store.record_llm(call, NOW - timedelta(days=100))
    session(store, tmp_path, detail_days=90).start()
    assert sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    assert store.summary(NOW, days=120)["tiles"]["calls"] == 1


def test_a_failing_session_start_never_raises(store: ObservabilityStore, tmp_path: Path) -> None:
    blocked = tmp_path / "logs"
    blocked.write_text("a file where the log directory should be")
    session(store, tmp_path).start()  # logged and dropped; the app still starts
