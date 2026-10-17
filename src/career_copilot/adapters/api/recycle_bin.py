"""HTTP for the recycle bin: what is in it, bring one back, or delete it for good.

A driving adapter only. What counts as "in the bin" and what a clean removes live in the
store; this maps HTTP onto those calls and reads the bin length from config.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config


def now() -> datetime:
    return datetime.now(UTC)


def _not_in_bin() -> HTTPException:
    return HTTPException(404, detail={"errors": {"jobId": "job is not in the recycle bin"}})


def router(get_config: Callable[[], Config]) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/bin")
    def recycle_bin() -> dict[str, object]:
        config = get_config()
        days = config.lifecycle.bin_days
        with SqliteStore(config.db_path) as store:
            items = store.bin_items(now(), days)
        return {
            "days": days,
            "items": [
                {
                    "jobId": item.job_id,
                    "title": item.title,
                    "company": item.company,
                    "dismissedAt": item.dismissed_at.isoformat(),
                    "daysLeft": item.days_left,
                }
                for item in items
            ],
        }

    @routes.post("/api/jobs/{job_id}/restore")
    def restore(job_id: int) -> dict[str, object]:
        with SqliteStore(get_config().db_path) as store:
            if not store.restore_job(job_id, now()):
                raise _not_in_bin()
        return {"jobId": job_id, "decision": None}

    @routes.delete("/api/bin/{job_id}", status_code=204)
    def delete_forever(job_id: int) -> Response:
        with SqliteStore(get_config().db_path) as store:
            if not store.in_bin(job_id):
                raise _not_in_bin()
            store.clean_job(job_id, now())
        return Response(status_code=204)

    return routes


def clean_expired(config: Config) -> int:
    """The clean that runs at server start, so an idle bin still empties on time."""
    with SqliteStore(config.db_path) as store:
        return store.clean_expired(now(), config.lifecycle.bin_days)
