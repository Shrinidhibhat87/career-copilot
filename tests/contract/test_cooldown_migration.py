from __future__ import annotations

import sqlite3
from pathlib import Path

from career_copilot.adapters.store import migrations


def test_migration_22_moves_72h_cooldowns_to_48h_and_leaves_others(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for version, sql in migrations.MIGRATIONS:
        if version <= 21:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?)", (version,))
    for slug, hours in (("a", 72), ("b", 24), ("c", 96)):
        conn.execute(
            "INSERT INTO source_targets (source, identifier, cooldown_hours, added_at)"
            " VALUES ('greenhouse', ?, ?, '2026-01-01')",
            (slug, hours),
        )
    conn.commit()
    migrations.migrate(conn)
    by_slug = {
        row["identifier"]: row["cooldown_hours"]
        for row in conn.execute("SELECT identifier, cooldown_hours FROM source_targets")
    }
    assert by_slug == {"a": 48, "b": 24, "c": 96}
