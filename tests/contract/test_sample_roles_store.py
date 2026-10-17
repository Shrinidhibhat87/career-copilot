"""Sample roles are text the agent reads later, so a removed one is archived rather than
deleted, the same posting added twice is one row, and crawling a sample role's board
never takes over a target the user already has for another reason.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.sample_roles import SampleRole

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
LATER = datetime(2026, 10, 8, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        yield store


def role(title: str, source_job_id: str | None = None) -> SampleRole:
    return SampleRole(
        id=0,
        source="ashby" if source_job_id else "manual",
        slug="taktile" if source_job_id else None,
        source_job_id=source_job_id,
        company="taktile",
        title=title,
        text=f"{title} text",
        url=f"https://jobs.ashbyhq.com/taktile/{source_job_id}" if source_job_id else None,
        added_at=NOW,
    )


def test_added_roles_list_newest_first(store: SqliteStore) -> None:
    first, created = store.add_sample_role(role("Forward Deployed Engineer", "a"), NOW)
    second, _ = store.add_sample_role(role("Solutions Engineer"), LATER)
    assert created
    assert first.id > 0
    assert second.added_at == LATER
    listed = store.active_sample_roles()
    assert [r.title for r in listed] == ["Solutions Engineer", "Forward Deployed Engineer"]
    assert listed[1] == first


def test_archived_role_is_not_listed_and_archives_once(store: SqliteStore) -> None:
    added, _ = store.add_sample_role(role("Forward Deployed Engineer", "a"), NOW)
    assert store.archive_sample_role(added.id, LATER)
    assert store.active_sample_roles() == []
    assert not store.archive_sample_role(added.id, LATER)
    assert not store.archive_sample_role(9999, LATER)


def test_same_posting_twice_returns_the_existing_row(store: SqliteStore) -> None:
    first, _ = store.add_sample_role(role("Forward Deployed Engineer", "a"), NOW)
    again, created = store.add_sample_role(role("Forward Deployed Engineer", "a"), LATER)
    assert not created
    assert again == first
    assert len(store.active_sample_roles()) == 1


def test_pasted_roles_are_never_deduplicated(store: SqliteStore) -> None:
    store.add_sample_role(role("Solutions Engineer"), NOW)
    _, created = store.add_sample_role(role("Solutions Engineer"), LATER)
    assert created
    assert len(store.active_sample_roles()) == 2


def test_re_adding_an_archived_posting_creates_a_new_row(store: SqliteStore) -> None:
    first, _ = store.add_sample_role(role("Forward Deployed Engineer", "a"), NOW)
    store.archive_sample_role(first.id, NOW)
    again, created = store.add_sample_role(role("Forward Deployed Engineer", "a"), LATER)
    assert created
    assert again.id != first.id


def origins(store: SqliteStore) -> list[tuple[str, int]]:
    rows = store._conn.execute(
        "SELECT origin, active FROM source_targets WHERE source='ashby' AND identifier='taktile'"
    ).fetchall()
    return [(row["origin"], row["active"]) for row in rows]


def test_sample_role_target_is_crawled(store: SqliteStore) -> None:
    store.add_sample_role_target("ashby", "taktile", NOW)
    assert origins(store) == [("sample_role", 1)]


def test_an_imported_target_keeps_its_origin_and_is_reactivated(store: SqliteStore) -> None:
    store.insert_imported_target(SourceTarget("ashby", "taktile"), NOW, external_id="1")
    store._conn.execute("UPDATE source_targets SET active = 0")
    store.add_sample_role_target("ashby", "taktile", LATER)
    assert origins(store) == [("imported", 1)]
