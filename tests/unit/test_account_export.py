"""The export is a whole copy; an import is checked before it can become an account."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from career_copilot.adapters import account_export
from career_copilot.adapters.store.migrations import MIGRATIONS
from career_copilot.adapters.store.sqlite_store import SqliteStore


def test_an_export_taken_while_the_app_writes_is_whole(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    SqliteStore(db).close()
    stop = threading.Event()

    def write() -> None:
        conn = sqlite3.connect(db, timeout=5)
        i = 0
        while not stop.is_set():
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (10_000 + i,))
            conn.commit()
            i += 1
        conn.close()

    writer = threading.Thread(target=write)
    writer.start()
    try:
        copy = account_export.export_copy(str(db))
    finally:
        stop.set()
        writer.join()
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
    copy.unlink()


def test_a_real_database_is_accepted_and_a_plain_file_is_not(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    SqliteStore(db).close()
    account_export.check_import(db)
    junk = tmp_path / "junk.db"
    junk.write_text("not a database at all")
    with pytest.raises(account_export.ImportRefused):
        account_export.check_import(junk)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(account_export.ImportRefused):
        account_export.check_import(other)


def test_a_database_from_a_newer_app_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    SqliteStore(db).close()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (MIGRATIONS[-1][0] + 1,))
    conn.commit()
    conn.close()
    with pytest.raises(account_export.ImportRefused, match="newer"):
        account_export.check_import(db)
