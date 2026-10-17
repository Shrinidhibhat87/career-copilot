"""Thread lifecycle and HTTP mapping for runs. SQLite connections stay thread-local."""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Annotated, Any

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query

from career_copilot.adapters.observability_session import AppSession
from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.adapters.store.sqlite_store import SqliteStore, process_started_at
from career_copilot.config import Config
from career_copilot.domain.runs import RunInProgress, stalled
from career_copilot.usecases.doctor import diagnose
from career_copilot.usecases.pipeline import (
    PipelineRun,
    current_preferences,
    finalize_error,
    freeze_config,
    now,
    preview_score,
)

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
cancel = Event()
active_future: Future[None] | None = None
SHUTDOWN_GRACE_SECONDS = 10
# The app refreshes on open when the last good run is older than this: a day's new
# postings, without a run on every page load.
STALE_AFTER = timedelta(hours=20)
# Set when a save asks for a refresh while a run is active. That run froze the old
# inputs, so one more run follows it rather than the request being dropped.
refresh_pending = Event()
# Every role a refresh may call. A refresh starts without a cost preview, so it is only
# allowed when none of them is billed.
REFRESH_ROLES = ("triage", "match", "profiler", "curator")
# `code` in a refresh answer that says a billed run needs the preview and approval flow.
BILLED_ROLES = "billed_roles"
# `ui_flags` name: true means the cloud-run privacy notice is shown before the next run.
CLOUD_NOTICE_FLAG = "cloud_notice_remind"


def worker(config: Config, run_id: int, cancel_event: Event) -> None:
    # PipelineRun.execute already guarantees a terminal row for anything raised inside
    # it. This wrapper exists only for what happens before or around that: opening the
    # store, or the interpreter tearing the thread down.
    try:
        with SqliteStore(config.db_path) as store:
            PipelineRun(store, config, run_id, cancel=cancel_event).execute()
    except BaseException as exc:
        with SqliteStore(config.db_path) as recovery:
            finalize_error(recovery, run_id, exc)
    finally:
        if refresh_pending.is_set() and not cancel_event.is_set():
            refresh_pending.clear()
            request_refresh(config)


def request_refresh(config: Config, *, if_stale: bool = False) -> dict[str, object]:
    """Start an incremental run with no cost preview, if it is free and useful.

    Called after the user saves an intent entry or a sample role, and by the app on open
    with `if_stale`. Never raises for an expected reason: it answers why it did not start.
    """
    global active_future
    roles = [r for r in REFRESH_ROLES if not config.role(r).is_local]  # type: ignore[arg-type]
    if roles:
        return {
            "started": False,
            "code": BILLED_ROLES,
            "reason": "automatic refresh runs only on local models",
        }
    with SqliteStore(config.db_path) as store:
        if store.current_cv() is None:
            return {"started": False, "reason": "upload a CV first"}
        last = store.last_succeeded_run_at()
        if if_stale and last is not None and now() - last < STALE_AFTER:
            return {"started": False, "reason": "recommendations are fresh"}
        report = diagnose(config)
        if not report.ok:
            return {"started": False, "reason": report.problem()}
        try:
            with store.transaction():
                frozen = freeze_config(config, store, current_preferences(store, config))
                run_id = store.begin_run(
                    "full", frozen, now(), os.getpid(), process_started_at(os.getpid())
                )
        except RunInProgress:
            refresh_pending.set()
            return {"started": False, "reason": "queued after the active run"}
        try:
            active_future = executor.submit(worker, config, run_id, cancel)
        except BaseException as exc:
            finalize_error(store, run_id, exc)
            return {"started": False, "reason": "could not start the worker"}
    return {"started": True, "runId": run_id}


_app_session: AppSession | None = None


def run_active() -> bool:
    return active_future is not None and not active_future.done()


def start_session(config: Config) -> None:
    """What server start used to do for the one database; now done at each sign in."""
    global _app_session
    with SqliteStore(config.db_path) as store:
        store.reclaim_dead_run(now())
        store.reclaim_stale_syncs(now())
    _app_session = AppSession(
        ObservabilityStore(config.db_path),
        config.observability,
        Path(config.db_path).resolve().parent / "logs",
        profile=config.profile.name,
    )
    _app_session.start()


def end_session() -> None:
    global _app_session
    if _app_session is not None:
        _app_session.end()
        _app_session = None


def make_lifespan() -> Callable[..., Any]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        global active_future, cancel, executor
        # A new lifecycle owns a new event; never clear an old worker's cancellation.
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
        cancel = Event()
        active_future = None
        try:
            yield
        finally:
            end_session()
            cancel.set()
            future = active_future
            pending: set[asyncio.Future[None]]
            if future is not None:
                _, pending = await asyncio.wait(
                    {asyncio.wrap_future(future)}, timeout=SHUTDOWN_GRACE_SECONDS
                )
            else:
                pending = set()
            # A pending worker may still spend. Leave its row and connection alone;
            # its own finally block or later dead-owner recovery will finalize it.
            executor.shutdown(wait=not pending, cancel_futures=True)

    return lifespan


def _stalled(row: dict[str, Any]) -> bool:
    """A sync has no heartbeat until its first company finishes, so it is measured from
    its start rather than reading as stalled at second zero."""
    if row["state"] == "running":
        return stalled(row["heartbeat_at"], now())
    if row["state"] == "syncing":
        return stalled(row["heartbeat_at"] or row["started_at"], now())
    return False


def run_payload(row: dict[str, Any]) -> dict[str, object]:
    return {
        "runId": row["id"],
        "state": row["state"],
        "kind": row["kind"],
        "stage": row["stage"],
        "completed": row["completed"],
        "total": row["total"],
        "costEur": row["cost_eur"],
        "budgetEur": row["budget_eur"],
        "stalled": _stalled(row),
        "failureCategory": row["failure_category"],
        "failureMessage": row["failure_message"],
        "sourceFailures": row["source_failures"],
        "summary": row.get("summary"),
        "companyCounts": row.get("company_counts"),
        "stages": row.get("stages") or [],
        "funnel": row.get("funnel"),
        "startedAt": row["started_at"],
        "endedAt": row["ended_at"],
        "failed": row["failed"],
    }


def router(get_config: Callable[[], Config]) -> APIRouter:
    routes = APIRouter()

    @routes.post("/api/runs/preview")
    def preview() -> dict[str, object]:
        config = get_config()
        report = diagnose(config)
        with SqliteStore(config.db_path) as store:
            result = preview_score(config, store, current_preferences(store, config))
        return {
            "candidatesNow": result.candidates_now,
            "estimatedCalls": result.estimated_calls,
            "reservedEur": result.reserved_eur,
            "ceilingEur": result.ceiling_eur,
            "mode": result.mode,
            "token": result.token,
            "modelBackendReady": report.ok,
            "modelBackendProblem": report.problem(),
            # A cloud role with no key: the page says "add a key" rather than a server error.
            "modelBackendKeyMissing": any(
                status.kind == "openrouter" and not status.usable for status in report.statuses
            ),
            # hybrid | cloud | add_key: what the page offers. Never applied for the user.
            "modelBackendSuggest": report.suggest,
        }

    @routes.post("/api/runs")
    def start(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        global active_future
        token, ceiling = body.get("token"), body.get("ceilingEur")
        errors: dict[str, str] = {}
        if not isinstance(token, str) or not token:
            errors["token"] = "preview the run first"
        if (
            isinstance(ceiling, bool)
            or not isinstance(ceiling, int | float)
            or not math.isfinite(ceiling)
            or ceiling <= 0
        ):
            errors["ceilingEur"] = "expected a positive finite spending ceiling"
        if errors:
            raise HTTPException(422, detail={"errors": errors})
        config = get_config().model_copy(deep=True)
        report = diagnose(config)
        if not report.ok:
            raise HTTPException(
                409,
                detail={
                    "errors": {"modelBackend": report.problem()},
                    "suggest": report.suggest,
                },
            )
        with SqliteStore(config.db_path) as store:
            try:
                # Serialize preference saves with token validation and the frozen insert.
                with store.transaction():
                    frozen = freeze_config(config, store, current_preferences(store, config))
                    if token != frozen.token() or ceiling != frozen.budget_eur:
                        raise HTTPException(409, detail={"errors": {"token": "preview is stale"}})
                    run_id = store.begin_run(
                        "full", frozen, now(), os.getpid(), process_started_at(os.getpid())
                    )
            except RunInProgress:
                raise HTTPException(
                    409, detail={"errors": {"run": "a run is already active"}}
                ) from None
            try:
                active_future = executor.submit(worker, config, run_id, cancel)
            except BaseException as exc:
                finalize_error(store, run_id, exc)
                raise HTTPException(
                    500, detail={"errors": {"run": "Could not start the worker."}}
                ) from None
        return {"runId": run_id, "state": "running"}

    @routes.get("/api/runs/cloud-notice")
    def cloud_notice() -> dict[str, object]:
        config = get_config()
        with SqliteStore(config.db_path) as store:
            remind = store.ui_flag(CLOUD_NOTICE_FLAG)
        providers = sorted(
            {
                config.role(r).kind  # type: ignore[arg-type]
                for r in REFRESH_ROLES
                if not config.role(r).is_local  # type: ignore[arg-type]
            }
        )
        return {
            # Shown until the user dismisses it with the reminder box left unchecked.
            "show": remind is None or remind,
            "redactionEnabled": config.pii.enabled,
            "providers": providers,
        }

    @routes.post("/api/runs/cloud-notice")
    def dismiss_cloud_notice(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        remind = body.get("remind")
        if not isinstance(remind, bool):
            raise HTTPException(422, detail={"errors": {"remind": "expected true or false"}})
        config = get_config()
        with SqliteStore(config.db_path) as store:
            store.set_ui_flag(CLOUD_NOTICE_FLAG, remind)
        return {"show": remind}

    @routes.post("/api/runs/refresh")
    def refresh(body: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, object]:
        if_stale = bool((body or {}).get("ifStale", False))
        return request_refresh(get_config().model_copy(deep=True), if_stale=if_stale)

    @routes.get("/api/runs")
    def history(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, object]]:
        with SqliteStore(get_config().db_path) as store:
            return [run_payload(row) for row in store.list_runs(limit)]

    @routes.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> dict[str, object]:
        with SqliteStore(get_config().db_path) as store:
            row = store.get_run(run_id)
            if row is None:
                raise HTTPException(404, detail={"errors": {"runId": "run not found"}})
            return run_payload(row)

    @routes.get("/api/jobs")
    def jobs(
        outcome: str | None = None,
        sort: str = "score",
        direction: str = "desc",
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, object]:
        if outcome not in (None, "pass", "reject", "manual_review"):
            raise HTTPException(422, detail={"errors": {"outcome": "unknown outcome"}})
        if sort not in ("score", "triage", "title", "company", "age", "cost"):
            raise HTTPException(422, detail={"errors": {"sort": "unknown sort"}})
        if direction not in ("asc", "desc"):
            raise HTTPException(422, detail={"errors": {"direction": "expected asc or desc"}})
        with SqliteStore(get_config().db_path) as store:
            return store.results_page(outcome, sort, direction, page, limit)

    return routes
