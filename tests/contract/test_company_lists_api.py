"""HTTP contract for connecting and syncing a curated company list."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import company_lists
from career_copilot.adapters.sources.notion import KEY_ENV as NOTION_KEY_ENV
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.usecases import import_sources


class CapturingExecutor:
    """Captures a sync submission instead of running it on a real thread.

    `tests/contract/test_runs_api.py` uses the same device for the full-pipeline
    worker: asserting through a real background thread racing a polling connection
    against the same SQLite file is both slow and, under rollback-journal mode, prone
    to spurious lock contention that has nothing to do with the behavior under test.
    A test that wants the worker's effect calls it directly, synchronously, instead.
    """

    def __init__(self) -> None:
        self.submissions: list[tuple[Any, ...]] = []

    def submit(self, function: Any, *args: Any) -> Future[None]:
        self.submissions.append((function, *args))
        return Future()


@pytest.fixture(autouse=True)
def no_notion_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's real .env must never leak a Notion key into this suite."""
    monkeypatch.delenv(NOTION_KEY_ENV, raising=False)


@pytest.fixture
def offline_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """One real board (`acme`/greenhouse), everything else a clean 404 — the same
    shape `tests/e2e/test_cli_sources_sync.py` uses to keep resolution offline."""

    def fetch(url: str) -> Any:
        if "acme" in url and "greenhouse" in url:
            return {"jobs": [{"id": 1, "title": "Data Engineer", "content": ""}]}
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            "not found", request=request, response=httpx.Response(404, request=request)
        )

    monkeypatch.setattr(import_sources, "http_fetcher", lambda *args, **kwargs: fetch)


@pytest.fixture
def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Config, CapturingExecutor]]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("targets: []\n", encoding="utf-8")
    monkeypatch.setenv("CAREER_COPILOT_CONFIG", str(config_path))

    config = Config(db_path=str(tmp_path / "jobs.db"))
    executor = CapturingExecutor()
    monkeypatch.setattr(company_lists, "_executor", executor)
    app = FastAPI()
    app.include_router(company_lists.router(lambda: config))
    with SqliteStore(config.db_path):
        pass
    yield TestClient(app), config, executor


def run_sync(executor: CapturingExecutor) -> dict[str, Any]:
    """Runs the one captured sync submission synchronously, then returns the finished
    run row straight from the store."""
    [(function, *args)] = executor.submissions
    function(*args)
    config: Config = args[0]
    run_id: int = args[1]
    with SqliteStore(config.db_path) as store:
        row = store.get_run(run_id)
    assert row is not None
    return row


def test_config_starts_empty_and_names_whether_a_notion_key_is_set(
    api: tuple[TestClient, Config, CapturingExecutor],
) -> None:
    client, _, _ = api
    body = client.get("/api/company-lists/config").json()
    assert body == {
        "notion": {"databaseId": ""},
        "csv": {"path": ""},
        "configured": None,
        "notionKeyConfigured": False,
    }


def test_saving_a_database_id_persists_to_config_yaml_without_touching_its_comments(
    api: tuple[TestClient, Config, CapturingExecutor], tmp_path: Path
) -> None:
    client, config, _ = api
    config_path = tmp_path / "config.yaml"
    config_path.write_text("# a hand-written comment\ntargets: []\n", encoding="utf-8")

    response = client.put("/api/company-lists/config", json={"notion": {"databaseId": "abc123"}})
    assert response.status_code == 200
    assert response.json()["configured"] == "notion"

    text = config_path.read_text()
    assert "# a hand-written comment" in text
    assert Config.load(config_path).company_lists.notion.database_id == "abc123"
    # Live for the very next request too, no restart needed.
    assert config.company_lists.notion.database_id == "abc123"


def test_uploading_a_csv_sets_the_path_and_reports_how_many_rows_it_read(
    api: tuple[TestClient, Config, CapturingExecutor],
) -> None:
    client, config, _ = api
    csv_body = b"name,url\nAcme Corp,https://boards.greenhouse.io/acme\nGhost Co,\n"
    response = client.post(
        "/api/company-lists/csv", files={"file": ("companies.csv", csv_body, "text/csv")}
    )
    assert response.status_code == 201
    assert response.json()["companies"] == 2
    assert config.company_lists.csv.path
    assert client.get("/api/company-lists/config").json()["configured"] == "csv"


def test_uploading_a_non_csv_file_is_refused(
    api: tuple[TestClient, Config, CapturingExecutor],
) -> None:
    client, _, _ = api
    response = client.post(
        "/api/company-lists/csv", files={"file": ("companies.txt", b"name\nAcme", "text/plain")}
    )
    assert response.status_code == 415


def test_sync_without_any_list_configured_is_refused(
    api: tuple[TestClient, Config, CapturingExecutor],
) -> None:
    client, _, _ = api
    assert client.post("/api/company-lists/sync", json={}).status_code == 422


def test_sync_against_notion_without_a_key_is_refused_before_any_request(
    api: tuple[TestClient, Config, CapturingExecutor],
) -> None:
    client, config, _ = api
    config.company_lists.notion.database_id = "db123"
    response = client.post("/api/company-lists/sync", json={"from": "notion"})
    assert response.status_code == 409
    assert NOTION_KEY_ENV in response.json()["detail"]["errors"]["notion"]


def test_sync_resolves_a_csv_watchlist_and_surfaces_what_could_not_be_resolved(
    api: tuple[TestClient, Config, CapturingExecutor], offline_fetcher: None, tmp_path: Path
) -> None:
    client, config, executor = api
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,url\nAcme Corp,https://boards.greenhouse.io/acme\nGhost Co,\n")
    config.company_lists.csv.path = str(csv_path)

    started = client.post("/api/company-lists/sync", json={"from": "csv"})
    assert started.status_code == 201
    assert started.json()["state"] == "syncing"
    # Before the worker runs, the run is visibly in flight rather than already succeeded.
    with SqliteStore(config.db_path) as store:
        in_flight = store.get_run(started.json()["runId"])
    assert in_flight is not None and in_flight["state"] == "syncing"
    run = run_sync(executor)

    assert run["state"] == "succeeded"
    assert run["kind"] == "company_sync"
    failures = {f["identifier"]: f["message"] for f in run["source_failures"]}
    assert "Ghost Co" in failures
    assert failures["Ghost Co"].startswith("no public board found (tried: ghost-co")
    assert "Acme Corp" not in failures  # resolved, not a failure

    with SqliteStore(config.db_path) as store:
        origins = store.imported_target_origins()
    assert origins[("greenhouse", "acme")] == "imported"


def test_sync_run_exposes_structured_counts_and_summary(
    api: tuple[TestClient, Config, CapturingExecutor], offline_fetcher: None, tmp_path: Path
) -> None:
    client, config, executor = api
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,url\nAcme Corp,https://boards.greenhouse.io/acme\nGhost Co,\n")
    config.company_lists.csv.path = str(csv_path)
    run_id = client.post("/api/company-lists/sync", json={"from": "csv"}).json()["runId"]
    run_sync(executor)

    from career_copilot.adapters.api.runs import run_payload

    with SqliteStore(config.db_path) as store:
        row = store.get_run(run_id)
    assert row is not None
    payload = run_payload(row)
    assert payload["companyCounts"] == {
        "fetched": 2,
        "resolved": 1,
        "unresolved": 1,
        "blocked": 0,
        "deactivated": 0,
    }
    assert payload["summary"] == "fetched 2: 1 resolved, 1 unresolved, 0 blocked, 0 deactivated"
    assert (row["completed"], row["total"]) == (2, 2)


def test_sync_failures_are_scoped_to_the_synced_list(
    api: tuple[TestClient, Config, CapturingExecutor], offline_fetcher: None, tmp_path: Path
) -> None:
    client, config, executor = api
    with SqliteStore(config.db_path) as store:
        from datetime import UTC, datetime

        from career_copilot.domain.sources import CompanyRef

        store.upsert_import_company("notion", CompanyRef("9", "Stale Notion Co"), datetime.now(UTC))
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,url\nGhost Co,\n")
    config.company_lists.csv.path = str(csv_path)
    client.post("/api/company-lists/sync", json={"from": "csv"})
    run = run_sync(executor)
    assert [f["identifier"] for f in run["source_failures"]] == ["Ghost Co"]


def test_summary_route_reports_current_counts_and_failures(
    api: tuple[TestClient, Config, CapturingExecutor], offline_fetcher: None, tmp_path: Path
) -> None:
    client, config, executor = api
    assert client.get("/api/company-lists/summary").json() == {
        "listSource": None,
        "counts": {"unresolved": 0, "resolved": 0, "blocked": 0, "inactive": 0},
        "openRoles": 0,
        "foundWithoutRoles": 0,
        "companies": [],
        "failures": [],
    }
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,url\nAcme Corp,https://boards.greenhouse.io/acme\nGhost Co,\n")
    config.company_lists.csv.path = str(csv_path)
    client.post("/api/company-lists/sync", json={"from": "csv"})
    run_sync(executor)
    body = client.get("/api/company-lists/summary").json()
    assert body["listSource"] == "csv"
    assert body["counts"]["resolved"] == 1
    assert body["counts"]["unresolved"] == 1
    (failure,) = body["failures"]
    assert (failure["name"], failure["status"]) == ("Ghost Co", "unresolved")
    assert failure["lastError"].startswith("no public board found (tried: ghost-co")


def test_summary_reports_open_roles_and_each_companys_standing(
    api: tuple[TestClient, Config, CapturingExecutor], offline_fetcher: None, tmp_path: Path
) -> None:
    client, config, executor = api
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,url\nAcme Corp,https://boards.greenhouse.io/acme\nGhost Co,\n")
    config.company_lists.csv.path = str(csv_path)
    client.post("/api/company-lists/sync", json={"from": "csv"})
    run_sync(executor)

    body = client.get("/api/company-lists/summary").json()
    by_name = {c["name"]: c for c in body["companies"]}
    assert by_name["Acme Corp"]["status"] == "resolved"
    assert by_name["Acme Corp"]["boardType"] == "greenhouse"
    assert by_name["Acme Corp"]["openRoles"] == body["openRoles"] > 0
    assert by_name["Acme Corp"]["lastError"] is None
    assert by_name["Ghost Co"]["openRoles"] is None
    assert by_name["Ghost Co"]["lastError"].startswith("no public board found")
    assert body["foundWithoutRoles"] == 0


def test_summary_counts_a_board_added_by_name(
    api: tuple[TestClient, Config, CapturingExecutor], offline_fetcher: None
) -> None:
    client, config, _ = api
    with SqliteStore(config.db_path) as store:
        store.add_manual_target(
            "greenhouse", "acme", datetime(2026, 10, 1, tzinfo=UTC), name="Acme Corp", open_roles=4
        )

    body = client.get("/api/company-lists/summary").json()

    assert body["counts"]["resolved"] == 1
    assert body["openRoles"] == 4
    (company,) = body["companies"]
    assert (company["name"], company["status"], company["boardType"]) == (
        "Acme Corp",
        "resolved",
        "greenhouse",
    )
    assert company["manual"] is True


def test_a_second_sync_while_one_is_running_joins_it(
    api: tuple[TestClient, Config, CapturingExecutor], offline_fetcher: None, tmp_path: Path
) -> None:
    client, config, executor = api
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,url\nAcme Corp,https://boards.greenhouse.io/acme\n")
    config.company_lists.csv.path = str(csv_path)

    first = client.post("/api/company-lists/sync", json={"from": "csv"}).json()
    second = client.post("/api/company-lists/sync", json={"from": "csv"}).json()

    assert second["runId"] == first["runId"]
    assert len(executor.submissions) == 1  # no rival worker was started

    run_sync(executor)
    third = client.post("/api/company-lists/sync", json={"from": "csv"}).json()
    assert third["runId"] != first["runId"]  # the first one finished, so this is a new sync
