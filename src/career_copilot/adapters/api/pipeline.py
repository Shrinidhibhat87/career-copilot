"""HTTP for the application board (spec 021).

The app is the source of truth: a stage, a note or a hide is saved first. A job that
already has a Notion row then gets its Status and Comments updated; a Notion failure is
stored on the card in plain words and never undoes the move.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from career_copilot.adapters.api.applications import _NOTION_LOCK, LogFactory, notion_log
from career_copilot.adapters.sources.notion import KEY_ENV, NotionError
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.pipeline import (
    LABELS,
    MAX_STALE_DAYS,
    NOTE_LIMIT,
    STAGES,
    arrange,
)


class StageBody(BaseModel):
    stage: str
    note: str | None = Field(default=None, max_length=NOTE_LIMIT)


class NoteBody(BaseModel):
    note: str = Field(max_length=NOTE_LIMIT)


class SettingsBody(BaseModel):
    staleDays: int = Field(ge=1, le=MAX_STALE_DAYS)


def now() -> datetime:
    return datetime.now(UTC)


def router(get_config: Callable[[], Config], log_factory: LogFactory = notion_log) -> APIRouter:
    routes = APIRouter()

    def card(store: SqliteStore, job_id: int) -> dict[str, object]:
        found = next(
            (c for c in store.application_cards(now(), store.stale_days()) if c["jobId"] == job_id),
            None,
        )
        if found is None:
            raise HTTPException(404, detail={"errors": {"jobId": "this job is not applied"}})
        return found

    def mirror(config: Config, store: SqliteStore, job_id: int) -> None:
        """Pushes the card to its Notion row, if it has one. Never raises."""
        page = store.application_page(job_id)
        if page is None:
            return
        current = card(store, job_id)
        error: str | None = None
        if not os.environ.get(KEY_ENV):
            error = f"Add {KEY_ENV} to .env, then restart the app."
        else:
            try:
                with _NOTION_LOCK:
                    log_factory(config, store.notion_database()).update(
                        page, LABELS[str(current["stage"])], str(current["note"])
                    )
            except NotionError as exc:
                error = str(exc)
        store.set_card_notion_error(job_id, error, now())

    @routes.get("/api/pipeline")
    def board() -> dict[str, object]:
        with SqliteStore(get_config().db_path) as store:
            days = store.stale_days()
            laid = arrange(store.application_cards(now(), days))
        return {
            "applied": laid.applied,
            "interviewing": laid.interviewing,
            "closed": {"offers": laid.offers, "folded": laid.folded},
            "hidden": laid.hidden,
            "staleDays": days,
        }

    @routes.post("/api/pipeline/{job_id}/stage")
    def set_stage(job_id: int, body: StageBody) -> dict[str, object]:
        if body.stage not in STAGES:
            raise HTTPException(422, detail={"errors": {"stage": "unknown stage"}})
        config = get_config()
        with SqliteStore(config.db_path) as store:
            try:
                store.record_stage(job_id, body.stage, body.note, now())
            except LookupError:
                raise HTTPException(
                    404, detail={"errors": {"jobId": "this job is not applied"}}
                ) from None
            mirror(config, store, job_id)
            return card(store, job_id)

    @routes.put("/api/pipeline/{job_id}/note")
    def set_note(job_id: int, body: NoteBody) -> dict[str, object]:
        config = get_config()
        with SqliteStore(config.db_path) as store:
            card(store, job_id)
            store.set_card_note(job_id, body.note, now())
            mirror(config, store, job_id)
            return card(store, job_id)

    @routes.post("/api/pipeline/{job_id}/hide")
    def hide(job_id: int) -> dict[str, object]:
        return _hidden(job_id, True)

    @routes.post("/api/pipeline/{job_id}/unhide")
    def unhide(job_id: int) -> dict[str, object]:
        return _hidden(job_id, False)

    def _hidden(job_id: int, value: bool) -> dict[str, object]:
        with SqliteStore(get_config().db_path) as store:
            card(store, job_id)
            store.set_card_hidden(job_id, value, now())
            return card(store, job_id)

    @routes.get("/api/pipeline/{job_id}/timeline")
    def timeline(job_id: int) -> list[dict[str, object]]:
        with SqliteStore(get_config().db_path) as store:
            card(store, job_id)
            return store.stage_timeline(job_id)

    @routes.get("/api/pipeline/settings")
    def get_settings() -> dict[str, int]:
        with SqliteStore(get_config().db_path) as store:
            return {"staleDays": store.stale_days()}

    @routes.put("/api/pipeline/settings")
    def put_settings(body: SettingsBody) -> dict[str, int]:
        with SqliteStore(get_config().db_path) as store:
            store.set_stale_days(body.staleDays)
        return {"staleDays": body.staleDays}

    return routes
