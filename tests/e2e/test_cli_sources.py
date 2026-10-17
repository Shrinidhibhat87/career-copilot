"""The `sources` commands: reviewing is the only path from a proposal to a crawl."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from career_copilot.__main__ import app
from career_copilot.adapters import cli_sources
from career_copilot.adapters.llm.backends import BackendStatus
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.sources import ProposalStatus, SourceProposal
from tests.accounts_helper import make_account


@pytest.fixture(autouse=True)
def account(tmp_path: Path) -> None:
    make_account(tmp_path)


NOW = datetime(2026, 9, 8, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commands call `load_dotenv()`, which reads the developer's real, untracked
    `.env` and undoes a test's `delenv`. Neutralising it keeps these tests about the
    environment the test itself sets, not the one the machine happens to have.
    """
    monkeypatch.setattr(cli_sources, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"db_path: {tmp_path / 'jobs.db'}\n"
        "targets:\n  - {source: greenhouse, identifier: seeded}\n",
        encoding="utf-8",
    )
    with SqliteStore(str(tmp_path / "users" / "test" / "jobs.db")) as store:
        store.insert_proposal(
            SourceProposal(
                board_type="greenhouse",
                slug="acme",
                company="Acme GmbH",
                evidence="search hit; probe returned 2 open jobs",
                job_count=2,
                sample_titles=("Data Engineer",),
            ),
            NOW,
        )
    return config


def invoke(config: Path, *args: str) -> Result:
    return runner.invoke(app, ["sources", *args, "--config", str(config)])


def test_review_lists_a_pending_proposal_with_its_evidence(workspace: Path) -> None:
    result = invoke(workspace, "review")
    assert result.exit_code == 0
    assert "Acme GmbH" in result.stdout and "2 open jobs" in result.stdout


def test_list_shows_only_the_configured_seed_until_something_is_approved(
    workspace: Path,
) -> None:
    result = invoke(workspace, "list")
    assert "greenhouse/seeded  (config" in result.stdout
    assert "acme" not in result.stdout


def test_approving_puts_the_board_into_the_crawled_list(workspace: Path) -> None:
    assert invoke(workspace, "review", "--approve", "1").exit_code == 0
    listing = invoke(workspace, "list").stdout
    assert "greenhouse/acme  (approved" in listing
    assert "greenhouse/seeded" in listing


def test_rejecting_keeps_the_row_and_crawls_nothing(workspace: Path) -> None:
    assert invoke(workspace, "review", "--reject", "1").exit_code == 0
    assert "no pending proposals" in invoke(workspace, "review").stdout
    assert "acme" not in invoke(workspace, "list").stdout
    with SqliteStore(str(workspace.parent / "users" / "test" / "jobs.db")) as store:
        assert store.list_proposals(ProposalStatus.REJECTED)[0].slug == "acme"


def test_deciding_a_proposal_twice_fails_loudly(workspace: Path) -> None:
    invoke(workspace, "review", "--approve", "1")
    result = invoke(workspace, "review", "--approve", "1")
    assert result.exit_code == 1 and "not pending" in result.stdout


def test_discover_without_a_search_key_says_so(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = invoke(workspace, "discover")
    assert result.exit_code == 1 and "TAVILY_API_KEY" in result.stdout


def test_discover_refuses_an_unavailable_model_before_building_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "search-key")
    monkeypatch.setattr(
        cli_sources,
        "probe",
        lambda role: BackendStatus(
            role.name, role.kind, role.model, role.base_url, False, False, detail="offline"
        ),
    )
    result = invoke(workspace, "discover")
    assert result.exit_code == 1
    assert "model" in result.stdout and "offline" in result.stdout


@pytest.mark.parametrize(
    "option,value",
    (("--target", "0"), ("--limit-searches", "0"), ("--budget", "0")),
)
def test_discovery_rejects_limits_that_would_disable_a_ceiling(
    workspace: Path, option: str, value: str
) -> None:
    result = invoke(workspace, "discover", option, value)
    assert result.exit_code == 2
