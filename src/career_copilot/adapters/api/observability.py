"""Call timing and token dashboards, read from the recorded rows."""

from __future__ import annotations

import gzip
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse

from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.config import Config

WINDOWS = (1, 7, 30, 90)  # 1 is today


def window(days: int) -> int:
    if days not in WINDOWS:
        raise HTTPException(422, detail=f"days must be one of {', '.join(map(str, WINDOWS))}")
    return days


def router(
    get_config: Callable[[], Config],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    api = APIRouter(prefix="/api/observability", tags=["observability"])

    def store() -> ObservabilityStore:
        return ObservabilityStore(get_config().db_path)

    @api.get("/summary")
    def summary(days: int = 7) -> dict[str, Any]:
        return store().summary(clock(), window(days))

    @api.get("/cost")
    def cost(days: int = 7) -> dict[str, Any]:
        return store().cost(clock(), window(days))

    @api.get("/tools")
    def tools(days: int = 7) -> list[dict[str, Any]]:
        return store().tools(clock(), window(days))

    @api.get("/flows")
    def flows(days: int = 7, limit: int = Query(5, ge=1, le=20)) -> list[dict[str, Any]]:
        return store().flows(clock(), window(days), limit)

    @api.get("/series")
    def series(days: int = 7) -> list[dict[str, Any]]:
        return store().series(clock(), window(days))

    @api.get("/sessions")
    def sessions() -> list[dict[str, Any]]:
        return store().sessions()

    @api.get("/sessions/{session_id}/log")
    def session_log(session_id: int) -> Response:
        """The log as a download. The path comes from the database row, never the request,
        and only files inside the logs folder are served."""
        found = store().session_log(session_id)
        if found is None:
            raise HTTPException(404, detail="no such session")
        path_text, archive = found
        logs_dir = Path(get_config().db_path).resolve().parent / "logs"
        path = Path(path_text).resolve()
        if path.is_relative_to(logs_dir) and path.is_file():
            return FileResponse(path, media_type="text/plain", filename=path.name)
        if archive is not None:
            return Response(
                gzip.decompress(archive),
                media_type="text/plain",
                headers={"Content-Disposition": f'attachment; filename="{Path(path_text).name}"'},
            )
        raise HTTPException(404, detail="this session's log is no longer available")

    return api
