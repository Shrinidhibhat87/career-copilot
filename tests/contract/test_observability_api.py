"""The observability endpoints over seeded rows: exact tiles, per-model rows, windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import observability
from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.config import Config
from career_copilot.ports import LlmCallRecord, ToolCallRecord

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def call(out: int | None, latency: float, outcome: str = "ok", model: str = "A") -> LlmCallRecord:
    return LlmCallRecord(
        "match", model, "ollama", "request", latency, outcome,
        tokens_in=100 if out is not None else None, tokens_out=out,
    )  # fmt: skip


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    store = ObservabilityStore(config.db_path)
    at = NOW - timedelta(days=1)
    for out, latency in ((50, 2.0), (100, 2.0), (150, 2.0)):
        store.record_llm(call(out, latency), at)
    store.record_llm(call(None, 1.0, "transport"), at)
    store.record_llm(call(10, 1.0, model="B"), NOW - timedelta(days=20))
    store.record_tool(ToolCallRecord("discovery", "probe_ats", 2.0, "ok"), at)
    store.record_tool(ToolCallRecord("discovery", "probe_ats", 4.0, "refused"), at)
    sid = store.open_session(NOW - timedelta(hours=1), None, "data/logs/app-x.log")
    store.close_session(sid, NOW, "clean", None)
    app = FastAPI()
    app.include_router(observability.router(lambda: config, clock=lambda: NOW))
    return TestClient(app)


def test_summary_returns_the_right_tiles_and_model_rows(client: TestClient) -> None:
    body = client.get("/api/observability/summary?days=7").json()
    assert body["tiles"] == {
        "tokensPerS": 42.86,
        "avgLatencyS": 1.75,
        "totalTokens": 600,
        "calls": 4,
        "errorRate": 0.25,
    }
    (row,) = body["rows"]
    assert (row["model"], row["stage"], row["calls"], row["tokensPerSP50"]) == (
        "A",
        "match",
        4,
        50.0,
    )


def test_a_wider_window_includes_older_calls(client: TestClient) -> None:
    body = client.get("/api/observability/summary?days=30").json()
    assert body["tiles"]["calls"] == 5
    assert {r["model"] for r in body["rows"]} == {"A", "B"}


def test_tools_series_and_sessions(client: TestClient) -> None:
    (tool,) = client.get("/api/observability/tools?days=7").json()
    assert (tool["tool"], tool["calls"], tool["slowestS"], tool["refused"], tool["errors"]) == (
        "probe_ats",
        2,
        4.0,
        1,
        0,
    )
    series = client.get("/api/observability/series?days=7").json()
    assert series == [{"day": "2026-10-07", "model": "A", "tokensPerS": 42.86}]
    (session,) = client.get("/api/observability/sessions").json()
    assert (session["outcome"], session["durationS"]) == ("clean", 3600.0)


def test_days_is_limited_to_the_dashboard_windows(client: TestClient) -> None:
    assert client.get("/api/observability/summary?days=5").status_code == 422
    assert client.get("/api/observability/summary?days=1").status_code == 200  # today
    assert client.get("/api/observability/summary").json()["days"] == 7  # the default


def test_an_empty_database_returns_empty_collections(tmp_path: Path) -> None:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    app = FastAPI()
    app.include_router(observability.router(lambda: config, clock=lambda: NOW))
    empty = TestClient(app)
    assert empty.get("/api/observability/summary").json()["rows"] == []
    assert empty.get("/api/observability/tools").json() == []
    assert empty.get("/api/observability/series").json() == []
    assert empty.get("/api/observability/sessions").json() == []


def test_flows_endpoint_returns_runs_with_steps(client: TestClient, tmp_path: Path) -> None:
    store = ObservabilityStore(str(tmp_path / "jobs.db"))
    at = NOW - timedelta(hours=2)
    store.record_tool(ToolCallRecord("curator", "get_job", 0.5, "ok", agent_run="r"), at)
    store.record_llm(
        LlmCallRecord("curator", "m", "ollama", "agent_run", 1.0, "ok", agent_run="r"),
        at + timedelta(seconds=2),
    )
    (flow,) = client.get("/api/observability/flows?days=7").json()
    assert flow["steps"][0]["tool"] == "get_job"
    assert client.get("/api/observability/flows?days=5").status_code == 422


def seed_session(tmp_path: Path, path: Path, gz: bytes | None = None) -> int:
    store = ObservabilityStore(str(tmp_path / "jobs.db"))
    sid = store.open_session(NOW, None, str(path))
    store.close_session(sid, NOW, "clean", gz)
    return sid


def test_a_session_log_downloads_as_a_file(client: TestClient, tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app-1.log").write_text("session start\nsession end\n")
    sid = seed_session(tmp_path, logs / "app-1.log")
    response = client.get(f"/api/observability/sessions/{sid}/log")
    assert response.status_code == 200
    assert response.text == "session start\nsession end\n"
    assert "app-1.log" in response.headers["content-disposition"]
    assert response.headers["content-type"].startswith("text/plain")


def test_an_archived_log_downloads_when_the_file_is_gone(
    client: TestClient, tmp_path: Path
) -> None:
    import gzip

    sid = seed_session(tmp_path, tmp_path / "logs" / "gone.log", gzip.compress(b"archived"))
    assert client.get(f"/api/observability/sessions/{sid}/log").text == "archived"


def test_a_log_outside_the_logs_folder_is_never_served(client: TestClient, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    sid = seed_session(tmp_path, secret)
    assert client.get(f"/api/observability/sessions/{sid}/log").status_code == 404


def test_an_unknown_session_log_is_404(client: TestClient) -> None:
    assert client.get("/api/observability/sessions/999/log").status_code == 404


def test_cost_endpoint_validates_the_window_and_returns_totals(client: TestClient) -> None:
    body = client.get("/api/observability/cost?days=7").json()
    assert body["days"] == 7
    assert body["totalEur"] == 0.0
    assert client.get("/api/observability/cost?days=5").status_code == 422
