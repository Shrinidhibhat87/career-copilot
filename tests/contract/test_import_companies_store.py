"""A curated watchlist is higher-trust input than a discovery proposal: a resolved row
goes straight into source_targets, keyed so a re-sync is idempotent and a company that
disappears from the list is deactivated, never deleted.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.sources import CompanyRef, ImportStatus, SourceProposal

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
LATER = datetime(2026, 9, 9, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        yield store


def test_a_new_row_starts_unresolved(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    row = store.get_import_company("csv", "1")
    assert row is not None
    assert row.status is ImportStatus.UNRESOLVED
    assert row.name == "Acme GmbH"


def test_re_upserting_refreshes_the_name_but_not_the_status(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.record_resolution("csv", "1", ImportStatus.RESOLVED, "greenhouse", "acme", None, NOW)
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH Renamed"), LATER)
    row = store.get_import_company("csv", "1")
    assert row is not None
    assert row.name == "Acme GmbH Renamed"
    assert row.status is ImportStatus.RESOLVED


def test_a_blocked_row_keeps_its_reason(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Firewalled Corp"), NOW)
    store.record_resolution(
        "csv", "1", ImportStatus.BLOCKED, None, None, "greenhouse/firewalled: timed out", NOW
    )
    row = store.get_import_company("csv", "1")
    assert row is not None
    assert row.status is ImportStatus.BLOCKED
    assert "timed out" in (row.last_error or "")


def test_a_resolved_company_lands_straight_in_the_active_crawl(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.insert_imported_target(SourceTarget("greenhouse", "acme", 0, 72), NOW, "1")
    targets = store.list_source_targets()
    assert [(t.source, t.identifier) for t in targets] == [("greenhouse", "acme")]
    assert store.imported_target_origins()[("greenhouse", "acme")] == "imported"


def test_a_company_missing_from_the_latest_fetch_is_deactivated_not_deleted(
    store: SqliteStore,
) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.record_resolution("csv", "1", ImportStatus.RESOLVED, "greenhouse", "acme", None, NOW)
    store.insert_imported_target(SourceTarget("greenhouse", "acme", 0, 72), NOW, "1")

    deactivated = store.deactivate_missing_import_companies("csv", (), LATER)

    assert deactivated == ("1",)
    assert store.get_import_company("csv", "1").status is ImportStatus.INACTIVE  # type: ignore[union-attr]
    assert store.list_source_targets() == []


def test_a_company_still_present_is_left_alone(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.record_resolution("csv", "1", ImportStatus.RESOLVED, "greenhouse", "acme", None, NOW)
    store.insert_imported_target(SourceTarget("greenhouse", "acme", 0, 72), NOW, "1")

    deactivated = store.deactivate_missing_import_companies("csv", ("1",), LATER)

    assert deactivated == ()
    assert len(store.list_source_targets()) == 1


def test_a_reappearing_company_starts_unresolved_again(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.record_resolution("csv", "1", ImportStatus.RESOLVED, "greenhouse", "acme", None, NOW)
    store.deactivate_missing_import_companies("csv", (), LATER)

    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), LATER)

    row = store.get_import_company("csv", "1")
    assert row is not None
    assert row.status is ImportStatus.UNRESOLVED


def test_the_same_external_id_on_another_list_source_is_a_different_row(
    store: SqliteStore,
) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.upsert_import_company("notion", CompanyRef("1", "Acme GmbH"), NOW)
    assert store.get_import_company("csv", "1") is not None
    assert store.get_import_company("notion", "1") is not None


def test_list_import_companies_filters_by_status(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.upsert_import_company("csv", CompanyRef("2", "Ghost AG"), NOW)
    store.record_resolution("csv", "1", ImportStatus.RESOLVED, "greenhouse", "acme", None, NOW)

    unresolved = store.list_import_companies("csv", statuses=[ImportStatus.UNRESOLVED])

    assert [row.external_id for row in unresolved] == ["2"]


def approve_a_board(store: SqliteStore, board_type: str, slug: str) -> None:
    """The other way a board reaches source_targets: an agent proposal the user approved."""
    store.insert_proposal(
        SourceProposal(
            board_type=board_type,
            slug=slug,
            company=slug.title(),
            evidence="search hit; probe returned 2 open jobs",
            job_count=2,
        ),
        NOW,
    )
    pending = store.list_proposals()[0]
    assert pending.id is not None
    store.approve_proposal(pending.id, NOW)


def test_importing_a_board_already_approved_does_not_take_over_its_origin(
    store: SqliteStore,
) -> None:
    """Otherwise dropping the company from the watchlist would later deactivate a
    target the user approved for an entirely separate reason."""
    approve_a_board(store, "greenhouse", "acme")
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)

    store.insert_imported_target(SourceTarget("greenhouse", "acme", 0, 72), NOW, "1")

    assert store.imported_target_origins()[("greenhouse", "acme")] == "approved"


def test_a_board_only_this_import_added_keeps_the_imported_origin(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.insert_imported_target(SourceTarget("greenhouse", "acme", 0, 72), NOW, "1")
    store.insert_imported_target(SourceTarget("greenhouse", "acme", 0, 72), LATER, "1")

    assert store.imported_target_origins()[("greenhouse", "acme")] == "imported"


def test_an_approved_board_survives_the_company_leaving_the_list(store: SqliteStore) -> None:
    approve_a_board(store, "greenhouse", "acme")
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), NOW)
    store.insert_imported_target(SourceTarget("greenhouse", "acme", 0, 72), NOW, "1")

    store.deactivate_missing_import_companies("csv", (), LATER)

    crawled = {(t.source, t.identifier) for t in store.list_source_targets()}
    assert ("greenhouse", "acme") in crawled


def test_company_sync_run_tags_its_list_and_scopes_failures(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Csv Ghost"), NOW)
    store.upsert_import_company("notion", CompanyRef("1", "Notion Ghost"), NOW)
    run_id = store.start_run(NOW, kind="company_sync")
    store.record_company_sync(
        run_id,
        "csv",
        {"fetched": 1, "resolved": 0, "unresolved": 1, "blocked": 0, "deactivated": 0},
    )
    run = store.get_run(run_id)
    assert run is not None
    assert [f["identifier"] for f in run["source_failures"]] == ["Csv Ghost"]
    assert run["company_counts"]["unresolved"] == 1


def test_a_homepage_is_stored_with_the_row(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH", "https://acme.example"), NOW)
    assert store.homepage_for_company("acme gmbh") == "https://acme.example"


def test_a_board_url_never_overwrites_a_known_homepage(store: SqliteStore) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH", "https://acme.example"), NOW)
    store.upsert_import_company(
        "csv", CompanyRef("1", "Acme GmbH", "https://boards.greenhouse.io/acme"), LATER
    )
    store.upsert_import_company("csv", CompanyRef("1", "Acme GmbH"), LATER)
    assert store.homepage_for_company("Acme GmbH") == "https://acme.example"


def test_a_board_url_alone_is_not_a_homepage(store: SqliteStore) -> None:
    store.upsert_import_company(
        "csv", CompanyRef("1", "Acme GmbH", "https://boards.greenhouse.io/acme"), NOW
    )
    assert store.homepage_for_company("Acme GmbH") is None
