"""`career-copilot sources sync`: a curated list reaches the crawl with no approval
step, and a company that cannot be verified is named rather than dropped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner, Result

from career_copilot.__main__ import app
from career_copilot.adapters import cli_sources
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.sources import ImportStatus
from career_copilot.usecases import import_sources
from tests.accounts_helper import make_account


@pytest.fixture(autouse=True)
def account(tmp_path: Path) -> None:
    make_account(tmp_path)


runner = CliRunner()


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """`load_dotenv()` would otherwise read the developer's real .env mid-test."""
    monkeypatch.setattr(cli_sources, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture
def boards(monkeypatch: pytest.MonkeyPatch) -> None:
    """`acme` is a real Greenhouse board, `blocked-co` is firewalled, everything else
    is a clean 404. The use case binds `http_fetcher` at import, so the patch has to
    land there rather than on the `ats` module, and the command stays offline.
    """

    def fetch(url: str) -> Any:
        if "acme" in url and "greenhouse" in url:
            return {"jobs": [{"id": 1, "title": "Data Engineer", "content": ""}]}
        if "blocked-co" in url:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                "forbidden", request=request, response=httpx.Response(403, request=request)
            )
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            "not found", request=request, response=httpx.Response(404, request=request)
        )

    monkeypatch.setattr(import_sources, "http_fetcher", lambda *args, **kwargs: fetch)


def workspace(tmp_path: Path, csv_body: str) -> Path:
    (tmp_path / "companies.csv").write_text(csv_body, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"db_path: {tmp_path / 'jobs.db'}\n"
        "targets: []\n"
        "company_lists:\n"
        f"  csv:\n    path: {tmp_path / 'companies.csv'}\n",
        encoding="utf-8",
    )
    return config


def invoke(config: Path, *args: str) -> Result:
    return runner.invoke(app, ["sources", *args, "--config", str(config)])


def test_a_resolved_company_is_reported_and_crawled(tmp_path: Path, boards: None) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\n")
    result = invoke(config, "sync")
    assert result.exit_code == 0, result.output
    assert "1 resolved (active)" in result.output
    assert "greenhouse/acme" in result.output
    assert "greenhouse/acme" in invoke(config, "list").output


def test_an_imported_target_is_labelled_imported_in_the_listing(
    tmp_path: Path, boards: None
) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\n")
    invoke(config, "sync")
    assert "(imported, tier 0)" in invoke(config, "list").output


def test_unresolved_and_blocked_are_reported_separately(tmp_path: Path, boards: None) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\nBlocked Co,\nGhost Startup,\n")
    result = invoke(config, "sync")
    assert result.exit_code == 0, result.output
    assert "1 resolved (active), 1 unresolved, 1 blocked" in result.output
    assert "blocked:" in result.output and "Blocked Co" in result.output
    assert "unresolved:" in result.output and "Ghost Startup" in result.output


def test_the_sync_is_recorded_as_a_company_sync_run(tmp_path: Path, boards: None) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\n")
    invoke(config, "sync")
    with SqliteStore(str(tmp_path / "users" / "test" / "jobs.db")) as store:
        runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["kind"] == "company_sync"
    assert runs[0]["state"] == "succeeded"
    assert "1 resolved" in runs[0]["summary"]


def test_a_second_sync_does_not_reprobe_a_resolved_company(tmp_path: Path, boards: None) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\n")
    invoke(config, "sync")
    result = invoke(config, "sync")
    assert "already resolved, not re-probed" in result.output
    assert "1 resolved (active)" in result.output


def test_a_company_removed_from_the_list_is_deactivated(tmp_path: Path, boards: None) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\n")
    invoke(config, "sync")
    (tmp_path / "companies.csv").write_text("name,url\nGhost Startup,\n", encoding="utf-8")
    result = invoke(config, "sync")
    assert "1 deactivated (removed from list)" in result.output
    assert "greenhouse/acme" not in invoke(config, "list").output
    with SqliteStore(str(tmp_path / "users" / "test" / "jobs.db")) as store:
        acme = next(c for c in store.list_import_companies("csv") if c.name == "Acme")
    assert acme.status is ImportStatus.INACTIVE


def test_dry_run_writes_nothing_and_probes_nothing(tmp_path: Path, boards: None) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\n")
    result = invoke(config, "sync", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "1 company(ies), nothing written" in result.output
    assert "· Acme" in result.output
    with SqliteStore(str(tmp_path / "users" / "test" / "jobs.db")) as store:
        assert store.list_import_companies() == []
        assert store.list_source_targets() == []


def test_refresh_reprobes_a_resolved_company(tmp_path: Path, boards: None) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\n")
    invoke(config, "sync")
    result = invoke(config, "sync", "--refresh")
    assert "already resolved, not re-probed" not in result.output
    assert "greenhouse/acme" in result.output


def test_an_unconfigured_list_fails_before_anything_is_built(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(f"db_path: {tmp_path / 'jobs.db'}\ntargets: []\n", encoding="utf-8")
    result = invoke(config, "sync")
    assert result.exit_code == 1
    assert "no company list is configured" in result.output


def test_notion_without_a_key_says_so_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    config = workspace(tmp_path, "name,url\nAcme,\n")
    result = invoke(config, "sync", "--from", "notion")
    assert result.exit_code == 1
    assert "NOTION_API_KEY" in result.output


def test_an_unknown_source_is_rejected(tmp_path: Path) -> None:
    config = workspace(tmp_path, "name,url\nAcme,\n")
    result = invoke(config, "sync", "--from", "airtable")
    assert result.exit_code == 1
    assert "unknown company list source" in result.output
