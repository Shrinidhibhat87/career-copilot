"""Moving an account's database to another machine as one file (spec 015, Part 2).

The export is a whole, consistent copy of `jobs.db`. An import is checked before anything is
created: it must be a career-copilot database no newer than this app.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from career_copilot.adapters.store.migrations import MIGRATIONS


class ImportRefused(ValueError):
    """The uploaded file cannot become an account; the message says why."""


def export_copy(db_path: str) -> Path:
    """A consistent copy taken with SQLite's backup, safe while the app is writing."""
    handle, name = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(name)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return Path(name)


def check_import(path: Path) -> None:
    """Raise `ImportRefused` unless `path` is a career-copilot database this app can open."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        raise ImportRefused("That file is not a Career Copilot export.") from None
    if row is None or row[0] is None:
        raise ImportRefused("That file is not a Career Copilot export.")
    if row[0] > MIGRATIONS[-1][0]:
        raise ImportRefused("That export comes from a newer version of Career Copilot.")
