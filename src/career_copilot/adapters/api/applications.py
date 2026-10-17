"""HTTP for logging an applied job to Notion.

Applied itself is a decision (`POST /api/jobs/{id}/decision`) and always saves first. This
route runs only after the user says Yes, and never touches the decision: a Notion failure
leaves the job Applied and says why in plain words.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from career_copilot.adapters.notion_applications import NotionApplicationLog, NotionPageMissing
from career_copilot.adapters.sources.notion import KEY_ENV, NotionError
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.ports import ApplicationEntry, ApplicationLog

LogFactory = Callable[[Config, tuple[str, str] | None], ApplicationLog]

# At most one Notion call at a time: two quick Yes clicks would otherwise both read the
# same next Index, and Notion rate-limits an integration well below what parallel calls hit.
_NOTION_LOCK = threading.Lock()


def now() -> datetime:
    return datetime.now(UTC)


def notion_log(config: Config, saved: tuple[str, str] | None) -> ApplicationLog:
    database_id, page_id = saved or (None, None)
    return NotionApplicationLog(
        parent_title=config.notion.applications_parent_title,
        database_title=config.notion.applications_database_title,
        database_id=database_id,
        page_id=page_id,
    )


def _error(status: int, message: str) -> HTTPException:
    return HTTPException(status, detail={"errors": {"notion": message}})


def router(get_config: Callable[[], Config], log_factory: LogFactory = notion_log) -> APIRouter:
    routes = APIRouter()

    @routes.post("/api/jobs/{job_id}/notion")
    def add_to_notion(job_id: int) -> dict[str, object]:
        config = get_config()
        with _NOTION_LOCK, SqliteStore(config.db_path) as store:
            detail = store.job_detail(job_id)
            if detail is None:
                raise HTTPException(404, detail={"errors": {"jobId": "job not found"}})
            existing = store.application_for_job(job_id)
            if existing is not None:
                return {"status": "already", "index": existing}
            if not os.environ.get(KEY_ENV):
                raise _error(409, f"Add {KEY_ENV} to .env, then restart the app.")
            index = store.next_application_index()
            reasons = detail.get("reasons")
            reasons = reasons if isinstance(reasons, list) else []
            entry = ApplicationEntry(
                index=index,
                role=str(detail["title"]),
                company=str(detail["company"]),
                source=str(detail["source"]),
                pilot_score=_score(detail.get("matchScore")),
                why=" ".join(str(r) for r in reasons) if reasons else str(detail["why"] or ""),
                applied_on=now().date(),
                comments="",
                status="Applied",
                job_url=str(detail["url"] or ""),
                location=str(detail["location"] or ""),
            )
            try:
                result = log_factory(config, store.notion_database()).record(entry)
            except NotionPageMissing as exc:
                raise _error(409, str(exc)) from None
            except NotionError as exc:
                raise _error(502, str(exc)) from None
            store.save_notion_database(result.database_id, result.parent_page_id)
            store.record_application(index, job_id, result.page_id, now())
        return {"status": "added", "index": index}

    return routes


def _score(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
