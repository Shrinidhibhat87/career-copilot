"""Download the signed-in account's database (spec 015, Part 2)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from career_copilot.adapters import account_export
from career_copilot.adapters.api import auth
from career_copilot.config import Config


def router(get_config: Callable[[], Config]) -> APIRouter:
    api = APIRouter(prefix="/api/account", tags=["account"])

    @api.get("/export")
    def export() -> FileResponse:
        copy = account_export.export_copy(get_config().db_path)
        return FileResponse(
            copy,
            media_type="application/octet-stream",
            filename=f"career-copilot-{auth.current_user() or 'account'}.db",
            background=BackgroundTask(copy.unlink),
        )

    return api
