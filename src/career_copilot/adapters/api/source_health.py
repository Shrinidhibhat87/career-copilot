"""HTTP for job-source health: each source's status, a test, and cutting a pause short.

A driving adapter only. What a status means, what a test sends and what a resume changes
live in `usecases/source_health.py` and `domain/source_health.py`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.ports import JobSource
from career_copilot.usecases.pipeline import build_sources
from career_copilot.usecases.source_health import (
    UnknownSource,
    resume_source,
    source_overview,
    try_source,
)

# A test is a real request to someone else's service, so a source is tested at most this
# often, however many times the button is pressed.
TEST_INTERVAL_SECONDS = 60.0


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _unknown(name: str) -> HTTPException:
    return HTTPException(404, detail={"errors": {"source": f"no job source named {name}"}})


def router(
    get_config: Callable[[], Config],
    sources: Callable[[], dict[str, JobSource]] = build_sources,
    clock: Callable[[], datetime] = _now,
    monotonic: Callable[[], float] = time.monotonic,
) -> APIRouter:
    routes = APIRouter(prefix="/api/sources", tags=["sources"])
    last_test: dict[str, float] = {}

    @routes.get("/health")
    def health() -> list[dict[str, object]]:
        config = get_config()
        with SqliteStore(config.db_path) as store:
            rows = source_overview(store, config, sources(), clock())
        return [
            {
                "source": row.source,
                "status": row.status,
                "targets": row.targets,
                "lastOkAt": _iso(row.last_ok_at),
                "lastFailure": row.last_failure,
                "lastMessage": row.last_message,
                "pausedUntil": _iso(row.paused_until),
                "fetchedLast7Days": row.fetched_last_7_days,
            }
            for row in rows
        ]

    @routes.post("/{name}/test")
    def test(name: str) -> dict[str, object]:
        adapters = sources()
        if name not in adapters:
            raise _unknown(name)
        wait = TEST_INTERVAL_SECONDS - (monotonic() - last_test.get(name, -1e9))
        if wait > 0:
            raise HTTPException(
                429,
                detail={
                    "errors": {"source": f"tested a moment ago; try again in {int(wait) + 1} s"}
                },
                headers={"Retry-After": str(int(wait) + 1)},
            )
        last_test[name] = monotonic()
        config = get_config()
        with SqliteStore(config.db_path) as store:
            result = try_source(store, config, adapters, name)
        return {
            "ok": result.ok,
            "fetched": result.fetched,
            "durationMs": result.duration_ms,
            "reason": result.reason,
            "message": result.message,
        }

    @routes.post("/{name}/resume")
    def resume(name: str) -> dict[str, str]:
        with SqliteStore(get_config().db_path) as store:
            try:
                return {"status": resume_source(store, sources(), name, clock())}
            except UnknownSource:
                raise _unknown(name) from None

    return routes
