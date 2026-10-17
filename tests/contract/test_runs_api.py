"""HTTP cost-gate, single-flight, polling, and results contracts."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import runs
from career_copilot.adapters.profiles import ProfileChoice
from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.runs import HEARTBEAT_TIMEOUT, RunConfig
from tests.conftest import make_raw


class CapturingExecutor:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.submissions: list[tuple[Any, ...]] = []

    def submit(self, function: Any, *args: Any) -> Future[None]:
        if self.failure is not None:
            raise self.failure
        self.submissions.append((function, *args))
        return Future()


@pytest.fixture
def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Config, CapturingExecutor]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    executor = CapturingExecutor()
    monkeypatch.setattr(runs, "executor", executor)
    monkeypatch.setattr(runs, "cancel", Event())
    monkeypatch.setattr(runs, "active_future", None)
    app = FastAPI()
    app.include_router(runs.router(lambda: config))
    return TestClient(app), config, executor


def test_preview_is_a_read_only_estimate_and_reports_backend_state(
    api: tuple[TestClient, Config, CapturingExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, _ = api
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with SqliteStore(config.db_path):
        pass
    response = client.post("/api/runs/preview")
    assert response.status_code == 200
    assert response.json()["modelBackendReady"] is False
    assert "OPENROUTER_API_KEY" in response.json()["modelBackendProblem"]
    assert response.json()["modelBackendKeyMissing"] is True
    with SqliteStore(config.db_path) as store:
        assert store._conn.total_changes == 0
        assert store.list_runs() == []


def test_start_rejects_stale_preview_and_missing_key(
    api: tuple[TestClient, Config, CapturingExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, executor = api
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    preview = client.post("/api/runs/preview").json()
    missing = client.post(
        "/api/runs", json={"token": preview["token"], "ceilingEur": preview["ceilingEur"]}
    )
    assert missing.status_code == 409
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config.scoring.match_model = "changed-after-preview"
    stale = client.post(
        "/api/runs", json={"token": preview["token"], "ceilingEur": preview["ceilingEur"]}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["errors"]["token"] == "preview is stale"
    assert executor.submissions == []


def test_a_paid_role_still_needs_a_valid_token_to_start(
    api: tuple[TestClient, Config, CapturingExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The settings page hides the approval card when every role is local. This is the gate
    # that card never was: with a paid role, the server refuses a start without the token.
    client, config, executor = api
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert not config.role("match").is_local
    ceiling = client.post("/api/runs/preview").json()["ceilingEur"]
    missing = client.post("/api/runs", json={"ceilingEur": ceiling})
    forged = client.post("/api/runs", json={"token": "forged", "ceilingEur": ceiling})
    assert missing.status_code >= 400
    assert forged.status_code >= 400
    assert executor.submissions == []


def test_start_accepts_a_ready_local_backend_without_a_remote_key(
    api: tuple[TestClient, Config, CapturingExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    from career_copilot.adapters.llm.backends import BackendStatus
    from career_copilot.usecases.doctor import DoctorReport

    client, _, executor = api
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ready = DoctorReport(
        (
            BackendStatus("triage", "ollama", "local", None, True, True),
            BackendStatus("match", "ollama", "local", None, True, True),
        )
    )
    monkeypatch.setattr(runs, "diagnose", lambda config: ready)
    preview = client.post("/api/runs/preview").json()
    response = client.post(
        "/api/runs", json={"token": preview["token"], "ceilingEur": preview["ceilingEur"]}
    )
    assert response.status_code == 200
    assert len(executor.submissions) == 1


def test_start_freezes_configuration_and_single_flight_is_a_database_guarantee(
    api: tuple[TestClient, Config, CapturingExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, executor = api
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    preview = client.post("/api/runs/preview").json()
    body = {"token": preview["token"], "ceilingEur": preview["ceilingEur"]}
    first = client.post("/api/runs", json=body)
    second = client.post("/api/runs", json=body)
    assert first.status_code == 200
    assert first.json()["state"] == "running"
    assert second.status_code == 409
    assert len(executor.submissions) == 1
    with SqliteStore(config.db_path) as store:
        row = store.get_run(first.json()["runId"])
        assert row is not None
        assert row["state"] == "running"
        assert store.run_config(row["id"]).match_model == config.scoring.match_model


def test_submission_failure_finalizes_the_created_row(
    api: tuple[TestClient, Config, CapturingExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, _ = api
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(runs, "executor", CapturingExecutor(RuntimeError("submit failed")))
    preview = client.post("/api/runs/preview").json()
    response = client.post(
        "/api/runs", json={"token": preview["token"], "ceilingEur": preview["ceilingEur"]}
    )
    assert response.status_code == 500
    with SqliteStore(config.db_path) as store:
        row = store.list_runs()[0]
        assert (row["state"], row["failure_category"]) == ("failed", "internal")


def test_jobs_are_filterable_sortable_and_paginated(
    api: tuple[TestClient, Config, CapturingExecutor],
) -> None:
    client, config, _ = api
    now = datetime(2026, 9, 6, tzinfo=UTC)
    with SqliteStore(config.db_path) as store:
        for index, title in enumerate(("Zulu Data Engineer", "Alpha Data Engineer")):
            posting = to_posting(
                make_raw(source_job_id=str(index), title=title, url=f"https://example.test/{index}")
            )
            store.upsert_job(posting, apply_hard_filters(posting), now)
    response = client.get("/api/jobs?outcome=pass&sort=title&direction=asc&page=1&limit=1")
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert [item["title"] for item in response.json()["items"]] == ["Alpha Data Engineer"]


def test_startup_reclaims_a_stale_run_after_its_owner_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    started = datetime(2026, 9, 6, tzinfo=UTC)
    frozen = RunConfig(Preferences(), "triage", "match", 1, "initial", 1.0, 1, 0.02, 0.2, 256, 1024)
    with SqliteStore(config.db_path) as store:
        run_id = store.begin_run("full", frozen, started, 999_999, "birth")

    def missing(pid: int, signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("career_copilot.adapters.store.sqlite_store.os.kill", missing)
    monkeypatch.setattr(runs, "now", lambda: started + timedelta(seconds=HEARTBEAT_TIMEOUT + 1))

    async def start_and_stop() -> None:
        async with runs.make_lifespan()(FastAPI()):
            runs.start_session(config)
            with SqliteStore(config.db_path) as store:
                row = store.get_run(run_id)
                assert row is not None
                assert (row["state"], row["failure_category"]) == ("failed", "interrupted")

    asyncio.run(start_and_stop())


def test_shutdown_never_finalizes_a_worker_that_outlives_the_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    started = datetime(2026, 9, 6, tzinfo=UTC)
    frozen = RunConfig(Preferences(), "triage", "match", 1, "initial", 1.0, 1, 0.02, 0.2, 256, 1024)
    with SqliteStore(config.db_path) as store:
        run_id = store.begin_run("full", frozen, started, os.getpid(), "birth")
    monkeypatch.setattr(runs, "SHUTDOWN_GRACE_SECONDS", 0)

    async def start_and_stop() -> None:
        async with runs.make_lifespan()(FastAPI()):
            runs.start_session(config)
            runs.active_future = Future()

    asyncio.run(start_and_stop())
    with SqliteStore(config.db_path) as store:
        row = store.get_run(run_id)
        assert row is not None
        assert row["state"] == "running"


def test_the_app_session_records_the_active_model_profile(tmp_path: Path) -> None:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    config.profile = ProfileChoice("nvidia-vllm-awq", "auto", "auto: NVIDIA GPU found")

    async def start_and_stop() -> None:
        async with runs.make_lifespan()(FastAPI()):
            runs.start_session(config)
            pass

    asyncio.run(start_and_stop())
    [session] = ObservabilityStore(config.db_path).sessions()
    assert session["profile"] == "nvidia-vllm-awq"
