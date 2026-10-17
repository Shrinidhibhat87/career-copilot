"""The gate and the sign-in routes, against the real session (spec 015)."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from career_copilot.adapters.api import app as api
from career_copilot.adapters.api import auth, runs
from career_copilot.config import Config
from tests.resume_fixtures import make_pdf

pytestmark = pytest.mark.real_auth


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setattr(api, "_config", Config(db_path=str(tmp_path / "jobs.db")))
    monkeypatch.setattr(api, "_redactor", None)
    monkeypatch.setattr(auth, "_session", None)
    with TestClient(api.app) as test_client:
        yield test_client
    runs.end_session()


def register(client: TestClient, name: str, passcode: str = "secret1") -> object:
    return client.post("/api/auth/register", json={"name": name, "passcode": passcode})


def test_the_api_is_closed_until_someone_signs_in(client: TestClient) -> None:
    assert client.get("/api/resume").status_code == 401
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/api/auth/status").json() == {
        "accounts": [],
        "user": None,
        "adoptsExisting": False,
    }


def test_registering_signs_in_and_a_wrong_cookie_does_not(client: TestClient) -> None:
    assert register(client, "alice").status_code == 200  # type: ignore[attr-defined]
    assert client.get("/api/resume").status_code == 404  # open, just no CV yet
    client.cookies.set(auth.COOKIE, "forged")
    assert client.get("/api/resume").status_code == 401


def test_a_wrong_passcode_and_an_unknown_name_look_the_same(client: TestClient) -> None:
    register(client, "alice")
    client.post("/api/auth/logout")
    wrong = client.post("/api/auth/login", json={"name": "alice", "passcode": "nope-nope"})
    unknown = client.post("/api/auth/login", json={"name": "ghost", "passcode": "secret1"})
    assert (wrong.status_code, wrong.json()) == (unknown.status_code, unknown.json())
    assert wrong.status_code == 401


def test_names_and_passcodes_are_validated(client: TestClient) -> None:
    assert register(client, "../evil").status_code == 422  # type: ignore[attr-defined]
    assert register(client, "alice", "short").status_code == 422  # type: ignore[attr-defined]
    register(client, "alice")
    client.post("/api/auth/logout")
    assert register(client, "alice").status_code == 409  # type: ignore[attr-defined]


def test_two_accounts_never_see_each_others_data(client: TestClient) -> None:
    register(client, "alice")
    cv = {"file": ("cv.pdf", make_pdf("Python engineer\nBerlin"), "application/pdf")}
    assert client.post("/api/resume", files=cv).status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/resume").status_code == 401
    register(client, "bob")
    assert client.get("/api/resume").status_code == 404
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"name": "alice", "passcode": "secret1"})
    assert client.get("/api/resume").json()["filename"] == "cv.pdf"


def test_signing_out_waits_for_an_active_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(client, "alice")
    monkeypatch.setattr(runs, "active_future", Future())
    assert client.post("/api/auth/logout").status_code == 409
    assert client.get("/api/auth/status").json()["user"] == "alice"


def test_the_first_account_adopts_the_old_database_and_says_so(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "jobs.db").write_text("old")
    assert client.get("/api/auth/status").json()["adoptsExisting"] is True


def test_status_names_the_person_only_to_the_holder_of_the_cookie(client: TestClient) -> None:
    register(client, "alice")
    assert client.get("/api/auth/status").json()["user"] == "alice"
    client.cookies.clear()
    assert client.get("/api/auth/status").json()["user"] is None


def _import(client: TestClient, name: str, data: bytes) -> object:
    return client.post(
        "/api/auth/import",
        data={"name": name, "passcode": "secret1"},
        files={"file": ("export.db", data, "application/octet-stream")},
    )


def test_an_export_from_one_account_becomes_another(client: TestClient, tmp_path: Path) -> None:
    register(client, "alice")
    cv = {"file": ("cv.pdf", make_pdf("Python engineer\nBerlin"), "application/pdf")}
    client.post("/api/resume", files=cv)
    exported = client.get("/api/account/export")
    assert exported.status_code == 200
    assert "career-copilot-alice.db" in exported.headers["content-disposition"]
    assert b"openrouter" not in exported.content.lower()
    client.post("/api/auth/logout")
    assert _import(client, "bob", exported.content).status_code == 200  # type: ignore[attr-defined]
    assert client.get("/api/resume").json()["filename"] == "cv.pdf"
    assert list((tmp_path / "users").glob("*.upload")) == []


def test_a_refused_import_leaves_no_account(client: TestClient, tmp_path: Path) -> None:
    response = _import(client, "bob", b"this is not a database")
    assert response.status_code == 422  # type: ignore[attr-defined]
    assert client.get("/api/auth/status").json()["accounts"] == []
    assert not (tmp_path / "users" / "bob").exists()
    assert list((tmp_path / "users").glob("*.upload")) == []
    assert client.get("/api/resume").status_code == 401


def test_an_import_cannot_take_a_name_that_exists(client: TestClient) -> None:
    register(client, "alice")
    exported = client.get("/api/account/export").content
    client.post("/api/auth/logout")
    assert _import(client, "alice", exported).status_code == 409  # type: ignore[attr-defined]


def _delete(client: TestClient, passcode: str) -> object:
    return client.request("DELETE", "/api/auth/account", json={"passcode": passcode})


def test_deleting_an_account_needs_the_passcode_and_removes_everything(
    client: TestClient, tmp_path: Path
) -> None:
    register(client, "alice")
    client.post("/api/resume", files={"file": ("cv.pdf", make_pdf("Python"), "application/pdf")})
    assert _delete(client, "wrong-one").status_code == 401  # type: ignore[attr-defined]
    assert (tmp_path / "users" / "alice").exists()
    assert _delete(client, "secret1").status_code == 204  # type: ignore[attr-defined]
    assert not (tmp_path / "users" / "alice").exists()
    assert client.get("/api/resume").status_code == 401
    assert client.get("/api/auth/status").json()["accounts"] == []


def test_deleting_needs_a_session_and_waits_for_an_active_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _delete(client, "secret1").status_code == 401  # type: ignore[attr-defined]
    register(client, "alice")
    monkeypatch.setattr(runs, "active_future", Future())
    assert _delete(client, "secret1").status_code == 409  # type: ignore[attr-defined]
    assert (tmp_path / "users" / "alice").exists()
