"""Type a name, see the board, confirm it. Nothing is crawled before the confirm."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import source_lookup
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config, TargetSpec
from career_copilot.domain.sources import CompanyRef, ImportStatus, SourceProposal

BOARD = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/greenhouse_board.json").read_text()
)


def fetch(url: str) -> Any:
    if "greenhouse" in url and "/acme/" in url + "/":
        return BOARD
    request = httpx.Request("GET", url)
    raise httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )


def agent(config: Config, name: str) -> list[SourceProposal]:
    return [
        SourceProposal(
            "ashby", "helsing", "Helsing", "careers page", 0.9, 12, ("Forward Deployed Engineer",)
        )
    ]


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Config]]:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    config = Config(db_path=str(tmp_path / "jobs.db"))
    app = FastAPI()
    app.include_router(source_lookup.router(lambda: config, fetch=fetch, web_search=agent))
    with SqliteStore(config.db_path):
        pass
    yield TestClient(app), config


def test_a_name_finds_its_board_and_changes_nothing(api: tuple[TestClient, Config]) -> None:
    client, config = api
    body = client.post("/api/sources/lookup", json={"name": "Acme"}).json()

    assert body["found"] is True
    assert (body["boardType"], body["slug"]) == ("greenhouse", "acme")
    assert body["openRoles"] > 0
    with SqliteStore(config.db_path) as store:
        assert list(store.list_source_targets()) == []


def test_an_unknown_name_offers_the_web_search(api: tuple[TestClient, Config]) -> None:
    client, _ = api
    body = client.post("/api/sources/lookup", json={"name": "Zzyzx"}).json()

    assert body["found"] is False
    assert body["canSearchWeb"] is True
    assert "zzyzx" in body["tried"]


def test_without_a_search_key_the_web_search_is_not_offered(
    api: tuple[TestClient, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    monkeypatch.delenv("TAVILY_API_KEY")

    body = client.post("/api/sources/lookup", json={"name": "Zzyzx"}).json()
    assert body["canSearchWeb"] is False
    assert client.post("/api/sources/lookup/search", json={"name": "Zzyzx"}).status_code == 409


def test_the_search_returns_boards_without_storing_them(api: tuple[TestClient, Config]) -> None:
    client, config = api
    body = client.post("/api/sources/lookup/search", json={"name": "Helsing"}).json()

    assert body["boards"][0]["slug"] == "helsing"
    assert body["boards"][0]["sampleTitles"] == ["Forward Deployed Engineer"]
    with SqliteStore(config.db_path) as store:
        assert list(store.list_source_targets()) == []


def test_confirm_adds_a_verified_board_only(api: tuple[TestClient, Config]) -> None:
    client, config = api

    bad = client.post(
        "/api/sources/lookup/confirm", json={"boardType": "greenhouse", "slug": "nope"}
    )
    assert bad.status_code == 422

    ok = client.post(
        "/api/sources/lookup/confirm", json={"boardType": "greenhouse", "slug": "acme"}
    )
    assert ok.status_code == 201
    with SqliteStore(config.db_path) as store:
        assert [(t.source, t.identifier) for t in store.list_source_targets()] == [
            ("greenhouse", "acme")
        ]


def test_confirm_keeps_the_typed_name_and_the_boards_list_shows_it(
    api: tuple[TestClient, Config],
) -> None:
    client, _ = api
    client.post(
        "/api/sources/lookup/confirm",
        json={"boardType": "greenhouse", "slug": "acme", "name": "Acme Corp"},
    )

    (board,) = client.get("/api/sources/boards").json()

    assert (board["name"], board["boardType"], board["slug"]) == ("Acme Corp", "greenhouse", "acme")
    assert board["origin"] == "manual"
    assert board["openRoles"] > 0
    assert board["addedAt"] is not None
    assert board["lastVisitedAt"] is None


def test_boards_list_puts_the_longest_waiting_board_first(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    now = datetime(2026, 10, 1, tzinfo=UTC)
    with SqliteStore(config.db_path) as store:
        store.add_manual_target("greenhouse", "acme", now, name="Acme")
        store.add_manual_target("lever", "beta", now, name="Beta")
        store.add_manual_target("ashby", "gamma", now, name="Gamma")
        store.set_cooldown("lever", "beta", now + timedelta(hours=5), hours=24)
        store.set_cooldown("greenhouse", "acme", now + timedelta(hours=1), hours=24)

    names = [board["name"] for board in client.get("/api/sources/boards").json()]

    assert names == ["Gamma", "Acme", "Beta"]  # never visited, then oldest visit


def test_adding_a_board_the_crawl_already_visits_changes_nothing(
    api: tuple[TestClient, Config],
) -> None:
    client, _ = api
    confirm = {"boardType": "greenhouse", "slug": "acme", "name": "Acme Corp"}
    first = client.post("/api/sources/lookup/confirm", json=confirm).json()
    assert first["alreadyListed"] is False

    again = client.post("/api/sources/lookup/confirm", json={**confirm, "name": "A Different Name"})

    assert again.status_code == 201
    assert again.json()["alreadyListed"] is True
    assert again.json()["openRoles"] is None
    (board,) = client.get("/api/sources/boards").json()
    assert (board["name"], board["origin"]) == ("Acme Corp", "manual")
    assert board["openRoles"] == first["openRoles"]


def test_adding_a_board_written_in_config_changes_nothing(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    config.targets.append(TargetSpec(source="greenhouse", identifier="acme"))

    body = client.post(
        "/api/sources/lookup/confirm", json={"boardType": "greenhouse", "slug": "acme"}
    ).json()

    assert body["alreadyListed"] is True
    with SqliteStore(config.db_path) as store:
        assert store.list_source_targets() == []


def test_a_board_for_a_company_the_list_could_not_find_fills_that_entry(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    now = datetime(2026, 10, 1, tzinfo=UTC)
    with SqliteStore(config.db_path) as store:
        store.upsert_import_company("csv", CompanyRef("acme", "Acme Corp"), now)
        store.record_resolution(
            "csv", "acme", ImportStatus.UNRESOLVED, None, None, "no public board found", now
        )

    body = client.post(
        "/api/sources/lookup/confirm",
        json={"boardType": "greenhouse", "slug": "acme", "name": "acme corp"},
    ).json()

    assert body["filledListEntry"] == "Acme Corp"
    with SqliteStore(config.db_path) as store:
        company = store.get_import_company("csv", "acme")
        assert company is not None
        assert (company.status, company.board_type, company.slug) == (
            ImportStatus.RESOLVED,
            "greenhouse",
            "acme",
        )
        (visit,) = store.list_board_visits()
        assert (visit.name, visit.origin) == ("Acme Corp", "imported")  # not a second row


def test_a_blank_name_is_422(api: tuple[TestClient, Config]) -> None:
    client, _ = api
    assert client.post("/api/sources/lookup", json={"name": "  "}).status_code == 422
