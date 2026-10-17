"""Pointing an unresolved company at its real board by hand: the answer is verified
before it is kept, it survives a `--refresh`, and a wrong one changes nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from career_copilot.__main__ import app
from career_copilot.adapters import cli_sources
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.sources import ImportStatus
from career_copilot.usecases import import_sources, set_board
from career_copilot.usecases.set_board import parse_board
from tests.accounts_helper import make_account


@pytest.fixture(autouse=True)
def account(tmp_path: Path) -> None:
    make_account(tmp_path)


runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BOARD = json.loads((FIXTURES / "greenhouse_board.json").read_text())
NOW = datetime(2026, 9, 8, tzinfo=UTC)


def fetch(url: str) -> Any:
    """Only `real-n26` exists, on Greenhouse. `shaky` is firewalled; all else is a 404."""
    if "/boards/real-n26/" in url:
        return BOARD
    request = httpx.Request("GET", url)
    status = 403 if "shaky" in url else 404
    raise httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status, request=request)
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_sources, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(import_sources, "http_fetcher", lambda *args, **kwargs: fetch)
    monkeypatch.setattr(set_board, "http_fetcher", lambda *args, **kwargs: fetch)


def workspace(tmp_path: Path) -> Path:
    (tmp_path / "companies.csv").write_text("name,url\nN26 Bank,\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"db_path: {tmp_path / 'jobs.db'}\ntargets: []\n"
        f"company_lists:\n  csv:\n    path: {tmp_path / 'companies.csv'}\n",
        encoding="utf-8",
    )
    return config


def sources(config: Path, *args: str) -> Any:
    return runner.invoke(app, ["sources", *args, "--config", str(config)])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ashby/n26", ("ashby", "n26")),
        ("  Greenhouse/n26/  ", ("greenhouse", "n26")),
        ("https://jobs.ashbyhq.com/n26", ("ashby", "n26")),
        ("https://jobs.lever.co/n26/abc", ("lever", "n26")),
        ("n26", None),
        ("nosuchvendor/n26", None),
        ("", None),
    ],
)
def test_a_board_is_read_from_vendor_slash_slug_or_a_careers_link(
    text: str, expected: tuple[str, str] | None
) -> None:
    assert parse_board(text) == expected


def test_a_company_the_resolver_missed_can_be_pointed_at_its_board(tmp_path: Path) -> None:
    config = workspace(tmp_path)
    assert "1 unresolved" in sources(config, "sync").output

    result = sources(config, "set-board", "n26 bank", "https://boards.greenhouse.io/real-n26")
    assert result.exit_code == 0, result.output
    assert "greenhouse/real-n26" in result.output

    with SqliteStore(str(tmp_path / "users" / "test" / "jobs.db")) as store:
        (company,) = store.list_import_companies("csv")
    assert company.status is ImportStatus.RESOLVED
    assert (company.board_type, company.slug, company.pinned) == ("greenhouse", "real-n26", True)
    assert company.open_roles and company.last_error is None
    assert "greenhouse/real-n26  (imported" in sources(config, "list").output


def test_a_pinned_board_survives_a_refresh(tmp_path: Path) -> None:
    config = workspace(tmp_path)
    sources(config, "sync")
    sources(config, "set-board", "N26 Bank", "greenhouse/real-n26")
    sources(config, "sync", "--refresh")
    with SqliteStore(str(tmp_path / "users" / "test" / "jobs.db")) as store:
        (company,) = store.list_import_companies("csv")
    assert (company.status, company.board_type, company.slug) == (
        ImportStatus.RESOLVED,
        "greenhouse",
        "real-n26",
    )


def test_a_pinned_board_that_disappears_is_reported_not_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = workspace(tmp_path)
    sources(config, "sync")
    sources(config, "set-board", "N26 Bank", "greenhouse/real-n26")
    monkeypatch.setattr(import_sources, "http_fetcher", lambda *a, **k: _gone)
    sources(config, "sync", "--refresh")
    with SqliteStore(str(tmp_path / "users" / "test" / "jobs.db")) as store:
        (company,) = store.list_import_companies("csv")
    assert company.status is ImportStatus.BLOCKED
    assert (company.board_type, company.slug, company.pinned) == ("greenhouse", "real-n26", True)
    assert "set by hand" in (company.last_error or "")


def _gone(url: str) -> Any:
    request = httpx.Request("GET", url)
    raise httpx.HTTPStatusError(
        "gone", request=request, response=httpx.Response(404, request=request)
    )


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("greenhouse/does-not-exist", "no board called 'does-not-exist'"),
        ("greenhouse/shaky", "Could not check greenhouse/shaky"),
        ("just some words", "not a board link I recognise"),
    ],
)
def test_a_wrong_answer_is_refused_and_changes_nothing(
    tmp_path: Path, answer: str, message: str
) -> None:
    config = workspace(tmp_path)
    sources(config, "sync")
    result = sources(config, "set-board", "N26 Bank", answer)
    assert result.exit_code == 1
    assert message in result.output
    with SqliteStore(str(tmp_path / "users" / "test" / "jobs.db")) as store:
        (company,) = store.list_import_companies("csv")
    assert company.status is ImportStatus.UNRESOLVED
    assert company.pinned is False


def test_an_unknown_company_name_is_refused(tmp_path: Path) -> None:
    config = workspace(tmp_path)
    sources(config, "sync")
    result = sources(config, "set-board", "Nobody Inc", "greenhouse/real-n26")
    assert result.exit_code == 1
    assert "not in the csv list" in result.output
