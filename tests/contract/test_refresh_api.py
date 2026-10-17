"""Automatic refresh: free runs only, started on save or on a stale app open."""

from __future__ import annotations

import os
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import intent, runs, sample_roles
from career_copilot.adapters.llm.backends import BackendStatus
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import BackendSpec, Config, ModelsSpec, RoleSpec
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.runs import RunConfig
from career_copilot.usecases.doctor import DoctorReport

READY = DoctorReport((BackendStatus("triage", "vllm", "local", None, True, True),))


class Executor:
    def __init__(self) -> None:
        self.submissions: list[tuple[Any, ...]] = []

    def submit(self, function: Any, *args: Any) -> Future[None]:
        self.submissions.append((function, *args))
        return Future()


def local(tmp_path: Path) -> Config:
    role = RoleSpec(backend="vllm", model="qwen3-4b-instruct")
    return Config(
        db_path=str(tmp_path / "jobs.db"),
        models=ModelsSpec(
            backends={"vllm": BackendSpec(kind="vllm", base_url="http://localhost:8001/v1")},
            roles={"triage": role, "match": role},
        ),
    )


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> Executor:
    fake = Executor()
    monkeypatch.setattr(runs, "executor", fake)
    monkeypatch.setattr(runs, "cancel", Event())
    monkeypatch.setattr(runs, "active_future", None)
    monkeypatch.setattr(runs, "refresh_pending", Event())
    monkeypatch.setattr(runs, "diagnose", lambda config: READY)
    return fake


def with_cv(config: Config) -> Config:
    with SqliteStore(config.db_path) as store:
        store.save_cv("cv.txt", "Built ML platforms.", datetime.now(UTC))
    return config


def client(config: Config) -> TestClient:
    app = FastAPI()
    app.include_router(runs.router(lambda: config))
    return TestClient(app)


def test_a_billed_backend_never_refreshes_on_its_own(tmp_path: Path, executor: Executor) -> None:
    config = with_cv(Config(db_path=str(tmp_path / "jobs.db")))
    body = client(config).post("/api/runs/refresh").json()
    assert body == {
        "started": False,
        "code": "billed_roles",
        "reason": "automatic refresh runs only on local models",
    }
    assert executor.submissions == []


def test_the_cloud_notice_shows_until_dismissed_without_a_reminder(tmp_path: Path) -> None:
    api = client(Config(db_path=str(tmp_path / "jobs.db")))
    first = api.get("/api/runs/cloud-notice").json()
    assert first["show"] is True and first["redactionEnabled"] is True
    assert first["providers"] == ["openrouter"]
    assert api.post("/api/runs/cloud-notice", json={"remind": True}).json() == {"show": True}
    assert api.get("/api/runs/cloud-notice").json()["show"] is True
    assert api.post("/api/runs/cloud-notice", json={"remind": False}).json() == {"show": False}
    assert api.get("/api/runs/cloud-notice").json()["show"] is False
    assert api.post("/api/runs/cloud-notice", json={"remind": "no"}).status_code == 422


def test_no_cv_means_no_refresh(tmp_path: Path, executor: Executor) -> None:
    body = client(local(tmp_path)).post("/api/runs/refresh").json()
    assert body["started"] is False and "CV" in body["reason"]


def test_a_local_backend_starts_a_run(tmp_path: Path, executor: Executor) -> None:
    config = with_cv(local(tmp_path))
    body = client(config).post("/api/runs/refresh", json={}).json()
    assert body["started"] is True and isinstance(body["runId"], int)
    assert len(executor.submissions) == 1


def test_a_fresh_run_is_not_repeated_on_app_open(tmp_path: Path, executor: Executor) -> None:
    config = with_cv(local(tmp_path))
    with SqliteStore(config.db_path) as store:
        frozen = RunConfig(Preferences(), "t", "m", 1, "initial", 1.0, 1, 0.0, 0.0, 256, 1024)
        run_id = store.begin_run("full", frozen, datetime.now(UTC), os.getpid(), None)
        store.finish_run(run_id, datetime.now(UTC) - timedelta(hours=2), "ok")
    body = client(config).post("/api/runs/refresh", json={"ifStale": True}).json()
    assert body == {"started": False, "reason": "recommendations are fresh"}
    # A save is not an app open: it refreshes however recent the last run was.
    assert client(config).post("/api/runs/refresh").json()["started"] is True


def test_a_save_during_a_run_is_queued_for_after_it(tmp_path: Path, executor: Executor) -> None:
    config = with_cv(local(tmp_path))
    api = client(config)
    assert api.post("/api/runs/refresh").json()["started"] is True
    body = api.post("/api/runs/refresh").json()
    assert body == {"started": False, "reason": "queued after the active run"}
    assert runs.refresh_pending.is_set()


def test_saving_intent_or_a_new_sample_role_asks_for_a_refresh(tmp_path: Path) -> None:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    calls: list[Config] = []
    app = FastAPI()
    app.include_router(intent.router(lambda: config, on_change=calls.append))
    app.include_router(sample_roles.router(lambda: config, on_change=calls.append))
    api = TestClient(app)
    entry = api.post("/api/intent", json={"text": "FDE roles in Berlin"}).json()
    api.delete(f"/api/intent/{entry['id']}")
    manual = {"title": "FDE", "company": "Taktile", "text": "Decision engines."}
    role = api.post("/api/sample-roles", json=manual).json()
    api.delete(f"/api/sample-roles/{role['id']}")
    assert len(calls) == 4
    api.post("/api/intent", json={"text": ""})  # rejected: nothing saved, no refresh
    assert len(calls) == 4
