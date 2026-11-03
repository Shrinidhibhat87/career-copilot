"""Markets over HTTP and in the store (spec 018). No network."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import markets
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[TestClient, Config]]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    SqliteStore(config.db_path).close()
    app = FastAPI()
    app.include_router(markets.router(lambda: config))
    yield TestClient(app), config


def test_nothing_saved_reads_as_the_default_and_unconfirmed(
    world: tuple[TestClient, Config],
) -> None:
    client, _ = world
    body = client.get("/api/markets").json()
    assert body["enabled"] == ["DE"]
    assert body["confirmed"] is False
    # The seed cities (berlin, munich, ...) point to Germany.
    assert body["proposal"] == ["DE"]
    ready = {p["code"]: p["ready"] for p in body["packs"]}
    assert ready["DE"] is True and ready["FR"] is False and ready["US"] is False


def test_confirming_saves_and_the_newest_version_wins(world: tuple[TestClient, Config]) -> None:
    client, config = world
    assert client.put("/api/markets", json={"codes": ["DE", "DE"]}).json()["confirmed"] is True
    with SqliteStore(config.db_path) as store:
        store.save_market_setting(("DE",), datetime(2030, 1, 1, tzinfo=UTC))
        saved = store.market_setting()
        count = store._conn.execute("SELECT COUNT(*) FROM market_settings").fetchone()[0]
    assert saved is not None and saved.codes == ("DE",) and saved.version == count == 2


@pytest.mark.parametrize("codes", [[], "DE", ["ZA"], ["FR"], ["DE", "XX"], [1]])
def test_a_planned_unknown_or_malformed_choice_is_refused(
    world: tuple[TestClient, Config], codes: object
) -> None:
    client, config = world
    assert client.put("/api/markets", json={"codes": codes}).status_code == 400
    with SqliteStore(config.db_path) as store:
        assert store.market_setting() is None


def test_migration_adds_the_table_to_an_older_database(tmp_path: Path) -> None:
    import sqlite3

    from career_copilot.adapters.store.migrations import MIGRATIONS, migrate

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    for version, sql in MIGRATIONS:
        if version < 34:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    assert migrate(conn) == 36
    assert conn.execute("SELECT COUNT(*) FROM market_settings").fetchone()[0] == 0
