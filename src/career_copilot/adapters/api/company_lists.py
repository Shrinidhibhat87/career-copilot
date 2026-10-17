"""HTTP for connecting and syncing a curated company list (Notion or CSV).

A driving adapter only: `usecases/import_sources.py` and the resolution logic do the
real work, the same composition-only style as `review.py`/`runs.py`. The sync itself
runs on a background thread so the request returns a run id immediately, the same
"start, then poll `/api/runs/{id}`" shape the frontend already uses for a full pipeline
run.
"""

from __future__ import annotations

import csv
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Body, File, HTTPException, UploadFile

from career_copilot.adapters.sources.company_lists import CsvCompanyList
from career_copilot.adapters.sources.notion import KEY_ENV as NOTION_KEY_ENV
from career_copilot.adapters.sources.notion import NotionCompanyList, NotionError
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import CompanyListsSpec, Config, CsvListSpec, NotionListSpec
from career_copilot.domain.runs import Progress
from career_copilot.domain.sources import ImportStatus
from career_copilot.ports import CompanyListProvider
from career_copilot.usecases.import_sources import CompanyListUnavailable, build_run
from career_copilot.usecases.set_board import SetBoardError, set_board

# Matches the resume upload's own cap (`app.py:MAX_BYTES`) — a personal watchlist CSV is
# nowhere near either limit, so one shared ceiling is enough.
MAX_CSV_BYTES = 5 * 1024 * 1024
DEFAULT_CSV_PATH = "data/company_list.csv"

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="company-sync")


def _now() -> datetime:
    return datetime.now(UTC)


def _config_path() -> str:
    # The same resolution `app.py` and `cli_sources.py` already use.
    return os.environ.get("CAREER_COPILOT_CONFIG", "config.yaml")


def _write_company_lists(spec: CompanyListsSpec) -> None:
    """Rewrites just the `company_lists:` block of config.yaml in place.

    config.yaml is hand-documented with extensive comments (see its `company_lists:`
    template block, shipped commented out). Round-tripping the whole file through
    `yaml.safe_dump` would silently discard every comment in it, so only the lines of
    this one block are touched; everything else in the file is left byte-for-byte alone.
    """
    path = Path(_config_path())
    text = path.read_text() if path.exists() else ""
    block = yaml.safe_dump({"company_lists": spec.model_dump()}, sort_keys=False)
    pattern = re.compile(r"^company_lists:\n(?:[ \t].*\n?)*", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(block, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n" + block
    path.write_text(text)


def _provider(config: Config, list_source: str) -> CompanyListProvider | None:
    """Mirrors `cli_sources.py::_provider` — `None` when the chosen source has nothing
    configured yet, so the caller refuses with one message instead of each adapter
    inventing its own."""
    if list_source == "notion":
        database_id = config.company_lists.notion.database_id
        return NotionCompanyList(database_id) if database_id else None
    path = config.company_lists.csv.path
    return CsvCompanyList(path) if path else None


def _sync_worker(config: Config, run_id: int, list_source: str) -> None:
    with SqliteStore(config.db_path) as store:
        try:
            store.record_company_sync(run_id, list_source)

            def progress(completed: int, total: int) -> None:
                store.update_run_progress(
                    run_id, Progress("resolving", completed, total, 0.0, 0.0), _now()
                )

            run = build_run(
                _provider(config, list_source),
                store,
                list_source,
                _now(),
                on_progress=progress,
            )
            report = run.execute()
            store.record_company_sync(run_id, list_source, report.counts)
            store.finish_run(run_id, _now(), report.summary)
        except (CompanyListUnavailable, NotionError, OSError) as exc:
            store.fail_run(run_id, _now(), "error", str(exc))
        except BaseException as exc:  # a run row must always end terminal
            store.fail_run(run_id, _now(), "error", str(exc))
            raise


def _config_payload(spec: CompanyListsSpec) -> dict[str, object]:
    return {
        "notion": {"databaseId": spec.notion.database_id},
        "csv": {"path": spec.csv.path},
        "configured": spec.configured(),
        "notionKeyConfigured": bool(os.environ.get(NOTION_KEY_ENV)),
    }


def router(get_config: Callable[[], Config]) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/company-lists/config")
    def read_config() -> dict[str, object]:
        return _config_payload(get_config().company_lists)

    @routes.get("/api/company-lists/summary")
    def summary() -> dict[str, object]:
        """Current ledger state for the connected list: counts by status, how many open
        roles the found boards hold, and every company with its standing."""
        list_source = get_config().company_lists.configured()
        counts = {status.value: 0 for status in ImportStatus}
        companies: list[dict[str, object]] = []
        failures: list[dict[str, str]] = []
        open_roles = 0
        found_without_roles = 0
        if list_source is not None:
            with SqliteStore(get_config().db_path) as store:
                for company in store.list_import_companies(list_source):
                    counts[company.status.value] += 1
                    if company.status is ImportStatus.RESOLVED:
                        open_roles += company.open_roles or 0
                        if company.open_roles == 0:
                            found_without_roles += 1
                    reason = company.last_error or "no public board found"
                    companies.append(
                        {
                            "id": company.id,
                            "name": company.name,
                            "pinned": company.pinned,
                            "status": company.status.value,
                            "boardType": company.board_type,
                            "openRoles": company.open_roles,
                            "lastError": reason
                            if company.status in (ImportStatus.UNRESOLVED, ImportStatus.BLOCKED)
                            else None,
                        }
                    )
                    if company.status in (ImportStatus.UNRESOLVED, ImportStatus.BLOCKED):
                        failures.append(
                            {
                                "name": company.name,
                                "status": company.status.value,
                                "lastError": reason,
                            }
                        )
        # A board added by name is in the crawl but not in the list, so it would never
        # show here. Count it as found; it has no ledger row, hence no id to act on.
        with SqliteStore(get_config().db_path) as store:
            added = [row for row in store.list_board_visits() if row.origin == "manual"]
        for board in added:
            counts["resolved"] += 1
            open_roles += board.open_roles or 0
            if board.open_roles == 0:
                found_without_roles += 1
            companies.append(
                {
                    "id": 0,
                    "name": board.name,
                    "pinned": False,
                    "manual": True,
                    "status": "resolved",
                    "boardType": board.source,
                    "openRoles": board.open_roles,
                    "lastError": None,
                }
            )
        return {
            "listSource": list_source,
            "counts": counts,
            "openRoles": open_roles,
            "foundWithoutRoles": found_without_roles,
            "companies": companies,
            "failures": failures,
        }

    @routes.post("/api/company-lists/companies/{company_id}/board")
    def set_company_board(
        company_id: int, body: Annotated[dict[str, Any], Body()]
    ) -> dict[str, object]:
        """The user names the board for a company the resolver could not find. It is
        checked against the real board first; a wrong answer is a 422 and changes nothing."""
        board = str(body.get("board") or "").strip()
        with SqliteStore(get_config().db_path) as store:
            company = store.get_import_company_by_id(company_id)
            if company is None:
                raise HTTPException(404, detail={"errors": {"company": "unknown company"}})
            try:
                board_type, slug, open_roles = set_board(store, company, board, _now())
            except SetBoardError as exc:
                raise HTTPException(422, detail={"errors": {"board": str(exc)}}) from None
        return {
            "id": company_id,
            "status": "resolved",
            "boardType": board_type,
            "slug": slug,
            "openRoles": open_roles,
            "pinned": True,
        }

    @routes.put("/api/company-lists/config")
    def write_config(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        config = get_config()
        database_id = str((body.get("notion") or {}).get("databaseId", "")).strip()
        csv_path = str((body.get("csv") or {}).get("path", "")).strip()
        spec = CompanyListsSpec(
            notion=NotionListSpec(database_id=database_id), csv=CsvListSpec(path=csv_path)
        )
        _write_company_lists(spec)
        # Live for the very next request — `Config` isn't frozen, and `get_config()`
        # hands back the same shared instance every router was mounted with.
        config.company_lists = spec
        return _config_payload(spec)

    @routes.post("/api/company-lists/csv", status_code=201)
    async def upload_csv(file: Annotated[UploadFile, File()]) -> dict[str, object]:
        filename = file.filename or ""
        if Path(filename).suffix.lower() != ".csv":
            raise HTTPException(415, detail={"errors": {"file": "expected a .csv file"}})
        data = await file.read(MAX_CSV_BYTES + 1)
        if len(data) > MAX_CSV_BYTES:
            raise HTTPException(
                413, detail={"errors": {"file": "That file is over the 5 MB limit."}}
            )

        config = get_config()
        target = Path(config.company_lists.csv.path or DEFAULT_CSV_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if not config.company_lists.csv.path:
            spec = CompanyListsSpec(
                notion=config.company_lists.notion, csv=CsvListSpec(path=str(target))
            )
            _write_company_lists(spec)
            config.company_lists = spec

        try:
            companies = CsvCompanyList(target).fetch()
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise HTTPException(422, detail={"errors": {"file": str(exc)}}) from None
        return {"path": str(target), "companies": len(companies)}

    @routes.post("/api/company-lists/sync", status_code=201)
    def sync(body: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, object]:
        body = body or {}
        config = get_config().model_copy(deep=True)
        requested = str(body.get("from") or "").strip()
        list_source = requested or config.company_lists.configured()
        if list_source is None:
            raise HTTPException(
                422, detail={"errors": {"from": "no company list is configured yet"}}
            )
        if list_source not in ("notion", "csv"):
            raise HTTPException(422, detail={"errors": {"from": "expected notion or csv"}})
        if list_source == "notion" and not os.environ.get(NOTION_KEY_ENV):
            raise HTTPException(
                409, detail={"errors": {"notion": f"set {NOTION_KEY_ENV} in .env first"}}
            )
        if _provider(config, list_source) is None:
            raise HTTPException(
                422, detail={"errors": {"from": f"{list_source} is not configured"}}
            )

        with SqliteStore(config.db_path) as store:
            # A second click (or a page reload) joins the sync already running instead of
            # starting a rival one.
            running = store.active_company_sync(list_source, _now())
            if running is not None:
                return {"runId": running, "state": "syncing"}
            run_id = store.start_run(_now(), kind="company_sync")
            store.record_company_sync(run_id, list_source)
        _executor.submit(_sync_worker, config, run_id, list_source)
        return {"runId": run_id, "state": "syncing"}

    return routes
