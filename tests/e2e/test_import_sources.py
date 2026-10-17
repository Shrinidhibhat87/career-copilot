"""A whole sync, offline. No network, no LLM, no search — the resolution outcome is
decided entirely by the fake fetcher, and a re-sync must not re-probe what already
resolved or lose track of what disappeared from the list.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.sources import CompanyRef, ImportStatus
from career_copilot.usecases.import_sources import ImportSourcesRun

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BOARD = json.loads((FIXTURES / "greenhouse_board.json").read_text())
NOW = datetime(2026, 9, 8, tzinfo=UTC)
LATER = datetime(2026, 9, 15, tzinfo=UTC)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


class FakeCompanyList:
    def __init__(self, refs: Sequence[CompanyRef]) -> None:
        self.refs = refs

    def fetch(self) -> Sequence[CompanyRef]:
        return self.refs


def fetcher(url: str) -> Any:
    """acme resolves; blocked-corp times out; everything else is a clean 404."""
    if "/boards/acme/" in url:
        return BOARD
    if "blocked-corp" in url:
        raise httpx.TimeoutException("timed out")
    raise _http_error(404)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        yield store


def test_a_sync_splits_companies_into_resolved_unresolved_and_blocked(store: SqliteStore) -> None:
    refs = [
        CompanyRef("a", "Acme"),
        CompanyRef("b", "Blocked Corp"),
        CompanyRef("c", "Some Startup"),
    ]
    report = ImportSourcesRun(FakeCompanyList(refs), store, "csv", NOW, fetcher=fetcher).execute()

    assert report.fetched == 3
    assert [ref.external_id for ref in report.resolved] == ["a"]
    assert [ref.external_id for ref, _ in report.blocked] == ["b"]
    assert [ref.external_id for ref in report.unresolved] == ["c"]


def test_a_resolved_company_is_crawled_from_the_very_next_run(store: SqliteStore) -> None:
    refs = [CompanyRef("a", "Acme")]
    ImportSourcesRun(FakeCompanyList(refs), store, "csv", NOW, fetcher=fetcher).execute()

    targets = store.list_source_targets()
    assert [(t.source, t.identifier) for t in targets] == [("greenhouse", "acme")]
    assert store.imported_target_origins()[("greenhouse", "acme")] == "imported"


def test_re_syncing_within_a_day_does_not_probe_anything_again(store: SqliteStore) -> None:
    calls = 0

    def counting_fetcher(url: str) -> Any:
        nonlocal calls
        calls += 1
        return fetcher(url)

    refs = [CompanyRef("a", "Acme"), CompanyRef("c", "Nowhere Inc")]
    ImportSourcesRun(FakeCompanyList(refs), store, "csv", NOW, fetcher=counting_fetcher).execute()
    first_calls = calls
    again = ImportSourcesRun(
        FakeCompanyList(refs), store, "csv", NOW + timedelta(hours=1), fetcher=counting_fetcher
    ).execute()

    assert calls == first_calls  # the hit and the clean miss are both still fresh
    assert [r.external_id for r in again.resolved] == ["a"]
    assert [r.external_id for r in again.unresolved] == ["c"]


def test_a_stale_resolved_company_re_checks_only_its_own_board(store: SqliteStore) -> None:
    urls: list[str] = []

    def recording_fetcher(url: str) -> Any:
        urls.append(url)
        return fetcher(url)

    refs = [CompanyRef("a", "Acme")]
    ImportSourcesRun(FakeCompanyList(refs), store, "csv", NOW, fetcher=recording_fetcher).execute()
    urls.clear()
    ImportSourcesRun(
        FakeCompanyList(refs), store, "csv", LATER, fetcher=recording_fetcher
    ).execute()

    assert len(urls) == 1 and "/boards/acme/" in urls[0]  # one request, not a slug search


def test_a_stale_miss_is_probed_again_and_a_blocked_one_always_is(store: SqliteStore) -> None:
    probed: list[str] = []

    def recording_fetcher(url: str) -> Any:
        probed.append(url)
        return fetcher(url)

    refs = [CompanyRef("b", "Blocked Corp"), CompanyRef("c", "Nowhere Inc")]
    run = ImportSourcesRun(FakeCompanyList(refs), store, "csv", NOW, fetcher=recording_fetcher)
    run.execute()
    probed.clear()
    ImportSourcesRun(
        FakeCompanyList(refs), store, "csv", NOW + timedelta(hours=1), fetcher=recording_fetcher
    ).execute()
    # An hour later the clean miss is skipped, the blocked one is retried.
    assert probed and all("blocked" in url for url in probed)
    probed.clear()
    ImportSourcesRun(
        FakeCompanyList(refs), store, "csv", LATER, fetcher=recording_fetcher
    ).execute()
    assert any("nowhere" in url for url in probed)


def test_the_open_role_count_is_recorded_and_refreshed(store: SqliteStore) -> None:
    refs = [CompanyRef("a", "Acme")]
    ImportSourcesRun(FakeCompanyList(refs), store, "csv", NOW, fetcher=fetcher).execute()
    row = store.get_import_company("csv", "a")
    assert row is not None
    assert (row.open_roles, row.last_probed_at) == (len(BOARD["jobs"]), NOW)

    emptied: dict[str, Any] = {"jobs": []}
    ImportSourcesRun(
        FakeCompanyList(refs), store, "csv", LATER, fetcher=lambda url: emptied
    ).execute()
    later = store.get_import_company("csv", "a")
    assert later is not None
    assert (later.status, later.open_roles) == (ImportStatus.RESOLVED, 0)


def test_the_report_keeps_list_order_however_probes_finish(store: SqliteStore) -> None:
    refs = [CompanyRef(str(i), f"Company {i}") for i in range(12)] + [CompanyRef("a", "Acme")]
    report = ImportSourcesRun(FakeCompanyList(refs), store, "csv", NOW, fetcher=fetcher).execute()
    assert [r.external_id for r in report.unresolved] == [str(i) for i in range(12)]
    assert [r.external_id for r in report.resolved] == ["a"]


def test_a_company_removed_from_the_list_is_deactivated_not_deleted(store: SqliteStore) -> None:
    first = [CompanyRef("a", "Acme"), CompanyRef("d", "New Corp")]
    ImportSourcesRun(FakeCompanyList(first), store, "csv", NOW, fetcher=fetcher).execute()

    second = [CompanyRef("d", "New Corp")]
    run = ImportSourcesRun(FakeCompanyList(second), store, "csv", LATER, fetcher=fetcher)
    report = run.execute()

    assert report.deactivated == ("a",)
    assert store.list_source_targets() == []
    assert store.get_import_company("csv", "a").status is ImportStatus.INACTIVE  # type: ignore[union-attr]


def test_refresh_forces_a_resolved_company_to_be_re_probed(store: SqliteStore) -> None:
    calls = 0

    def counting_fetcher(url: str) -> Any:
        nonlocal calls
        calls += 1
        return fetcher(url)

    refs = [CompanyRef("a", "Acme")]
    ImportSourcesRun(FakeCompanyList(refs), store, "csv", NOW, fetcher=counting_fetcher).execute()
    first_calls = calls
    ImportSourcesRun(
        FakeCompanyList(refs), store, "csv", LATER, fetcher=counting_fetcher, refresh=True
    ).execute()

    assert calls > first_calls
