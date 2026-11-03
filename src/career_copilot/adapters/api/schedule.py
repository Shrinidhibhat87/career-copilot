"""HTTP for the account's run schedule (spec 019). A driving adapter only: when a slot is
due lives in `domain/schedule.py`, the ticker in `adapters/scheduler.py`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from career_copilot.adapters.api import runs
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.schedule import DEFAULT_SLOTS, next_slot, validate_slots
from career_copilot.usecases.doctor import diagnose
from career_copilot.usecases.model_settings import in_force


def _why_not(config: Config, store: SqliteStore) -> str | None:
    """Why a scheduled run would be skipped right now; the same gates `request_refresh` uses."""
    if any(not config.role(r).is_local for r in runs.REFRESH_ROLES):  # type: ignore[arg-type]
        return "scheduled runs only use local models"
    if store.current_cv() is None:
        return "upload a CV first"
    report = diagnose(config)
    return None if report.ok else report.problem()


def router(get_config: Callable[[], Config]) -> APIRouter:
    routes = APIRouter(prefix="/api/schedule", tags=["schedule"])

    def view(config: Config) -> dict[str, Any]:
        with SqliteStore(config.db_path) as store:
            saved = store.schedule_setting()
            last = store.last_run_started_at("scheduled")
            why = _why_not(in_force(config), store)
        slots = saved.slots if saved else DEFAULT_SLOTS
        times = saved.times if saved else ()
        upcoming = next_slot(times, datetime.now(UTC).astimezone()) if times else None
        skip = runs.last_skip()
        return {
            "enabled": bool(saved and saved.enabled),
            "times": list(times),
            "slots": [{"time": s.time, "on": s.on} for s in slots],
            "next": upcoming.isoformat() if saved and saved.enabled and upcoming else None,
            "lastRunAt": last.isoformat() if last else None,
            "lastSkip": {"reason": skip[0], "at": skip[1].isoformat()} if skip else None,
            "canRun": why is None,
            "whyNot": why,
        }

    @routes.get("")
    def get() -> dict[str, Any]:
        return view(get_config())

    @routes.put("")
    def put(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(422, detail={"errors": {"enabled": "expected true or false"}})
        try:
            slots = validate_slots(body.get("slots"))
        except ValueError as error:
            raise HTTPException(422, detail={"errors": {"slots": str(error)}}) from None
        if enabled and not any(s.on for s in slots):
            raise HTTPException(422, detail={"errors": {"slots": "switch on at least one slot"}})
        config = get_config()
        with SqliteStore(config.db_path) as store:
            store.save_schedule(enabled, slots, datetime.now(UTC))
        return view(config)

    return routes
