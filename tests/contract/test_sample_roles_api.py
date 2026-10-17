import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import sample_roles
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config

FIXTURES = Path(__file__).parent.parent / "fixtures"
FDE_URL = "https://jobs.ashbyhq.com/taktile/594e7ab1-3c77-43b5-be3f-bc30d513c14c"


def ashby(url: str) -> Any:
    return json.loads((FIXTURES / "ashby_board.json").read_text())


def down(url: str) -> Any:
    raise httpx.ConnectError("boom")


def make(tmp_path: Path, fetch: Any) -> tuple[TestClient, Config]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    app = FastAPI()
    app.include_router(sample_roles.router(lambda: config, fetcher_factory=lambda: fetch))
    return TestClient(app), config


def test_url_is_fetched_stored_and_its_board_crawled(tmp_path: Path) -> None:
    client, config = make(tmp_path, ashby)
    response = client.post("/api/sample-roles", json={"url": FDE_URL})
    assert response.status_code == 201
    role = response.json()
    assert role["title"] == "Forward Deployed Engineer"
    assert role["source"] == "ashby"
    assert role["url"] == FDE_URL
    assert client.get("/api/sample-roles").json()["roles"][0]["id"] == role["id"]
    with SqliteStore(config.db_path) as store:
        origins = store._conn.execute(
            "SELECT origin FROM source_targets WHERE source='ashby' AND identifier='taktile'"
        ).fetchall()
    assert [r["origin"] for r in origins] == ["sample_role"]


def test_same_url_twice_is_one_row(tmp_path: Path) -> None:
    client, _ = make(tmp_path, ashby)
    first = client.post("/api/sample-roles", json={"url": FDE_URL}).json()
    second = client.post("/api/sample-roles", json={"url": FDE_URL})
    assert second.json()["id"] == first["id"]
    assert len(client.get("/api/sample-roles").json()["roles"]) == 1


@pytest.mark.parametrize(
    ("fetch", "url"),
    [
        (ashby, "https://www.linkedin.com/jobs/view/123"),
        (ashby, "https://jobs.ashbyhq.com/taktile/99999999-0000-0000-0000-000000000000"),
        (down, FDE_URL),
    ],
)
def test_unusable_url_asks_for_text_and_stores_nothing(
    tmp_path: Path, fetch: Any, url: str
) -> None:
    client, config = make(tmp_path, fetch)
    response = client.post("/api/sample-roles", json={"url": url})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["needsText"] is True
    assert detail["errors"]["url"]
    assert client.get("/api/sample-roles").json()["roles"] == []
    with SqliteStore(config.db_path) as store:
        assert store._conn.execute("SELECT COUNT(*) FROM source_targets").fetchone()[0] == 0


def test_pasted_text_is_stored_as_manual(tmp_path: Path) -> None:
    client, _ = make(tmp_path, down)
    response = client.post(
        "/api/sample-roles",
        json={"title": "Solutions Engineer", "text": "Customer-facing ML.", "company": "Acme"},
    )
    assert response.status_code == 201
    assert response.json()["source"] == "manual"
    assert response.json()["url"] is None


def test_pasted_role_needs_title_and_text(tmp_path: Path) -> None:
    client, _ = make(tmp_path, down)
    response = client.post("/api/sample-roles", json={"title": "", "text": ""})
    assert response.status_code == 422
    assert set(response.json()["detail"]["errors"]) == {"title", "text"}


def test_delete_archives(tmp_path: Path) -> None:
    client, _ = make(tmp_path, ashby)
    role_id = client.post("/api/sample-roles", json={"url": FDE_URL}).json()["id"]
    assert client.delete(f"/api/sample-roles/{role_id}").status_code == 204
    assert client.get("/api/sample-roles").json()["roles"] == []
    assert client.delete(f"/api/sample-roles/{role_id}").status_code == 404
