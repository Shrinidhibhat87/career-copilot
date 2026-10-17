"""Mounting the frontend build. A missing build is a message, not a crash."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api.static_ui import mount_ui


def test_an_unbuilt_frontend_leaves_the_api_running(tmp_path: Path) -> None:
    app = FastAPI()
    assert mount_ui(app, tmp_path) is False


def test_a_built_frontend_is_served_at_the_root(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html>career copilot</html>", encoding="utf-8")
    app = FastAPI()

    @app.get("/api/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    assert mount_ui(app, tmp_path) is True
    client = TestClient(app)
    assert "career copilot" in client.get("/").text
    # The mount is at "/", so the API routes registered before it must still win.
    assert client.get("/api/ping").json() == {"ok": True}
