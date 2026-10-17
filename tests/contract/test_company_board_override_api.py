"""HTTP contract for naming a company's board by hand."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import company_lists
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.sources import CompanyRef, ImportStatus
from career_copilot.usecases import set_board

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def fetch(url: str) -> Any:
    if "/boards/real-n26/" in url:
        return {"jobs": [{"id": 1, "title": "Data Engineer", "content": ""}]}
    request = httpx.Request("GET", url)
    raise httpx.HTTPStatusError(
        "not found", request=request, response=httpx.Response(404, request=request)
    )


@pytest.fixture
def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Config, int]]:
    monkeypatch.setattr(set_board, "http_fetcher", lambda *args, **kwargs: fetch)
    config = Config(db_path=str(tmp_path / "jobs.db"))
    config.company_lists.csv.path = str(tmp_path / "companies.csv")
    app = FastAPI()
    app.include_router(company_lists.router(lambda: config))
    with SqliteStore(config.db_path) as store:
        store.upsert_import_company("csv", CompanyRef("n26", "N26 Bank"), NOW)
        store.record_resolution(
            "csv", "n26", ImportStatus.UNRESOLVED, None, None, "no public board found", NOW
        )
        company = store.get_import_company("csv", "n26")
        assert company is not None
    yield TestClient(app), config, company.id


def test_summary_gives_each_company_an_id_to_address_it_by(
    api: tuple[TestClient, Config, int],
) -> None:
    client, _, company_id = api
    (company,) = client.get("/api/company-lists/summary").json()["companies"]
    assert (company["id"], company["pinned"]) == (company_id, False)


def test_naming_the_board_resolves_and_pins_the_company(
    api: tuple[TestClient, Config, int],
) -> None:
    client, _, company_id = api
    response = client.post(
        f"/api/company-lists/companies/{company_id}/board",
        json={"board": "https://boards.greenhouse.io/real-n26"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": company_id,
        "status": "resolved",
        "boardType": "greenhouse",
        "slug": "real-n26",
        "openRoles": 1,
        "pinned": True,
    }
    body = client.get("/api/company-lists/summary").json()
    assert body["counts"]["resolved"] == 1 and body["failures"] == []
    assert body["companies"][0]["pinned"] is True


def test_a_board_that_does_not_exist_is_a_422_that_names_the_problem(
    api: tuple[TestClient, Config, int],
) -> None:
    client, _, company_id = api
    response = client.post(
        f"/api/company-lists/companies/{company_id}/board", json={"board": "greenhouse/nope"}
    )
    assert response.status_code == 422
    assert "no board called 'nope'" in response.json()["detail"]["errors"]["board"]
    assert client.get("/api/company-lists/summary").json()["counts"]["unresolved"] == 1


def test_an_unknown_company_is_a_404(api: tuple[TestClient, Config, int]) -> None:
    client, _, _ = api
    response = client.post("/api/company-lists/companies/9999/board", json={"board": "x/y"})
    assert response.status_code == 404
