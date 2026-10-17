"""`ApplicationLog` over a Notion "Applications" database the app finds or creates itself.

The read side (`adapters/sources/notion.py`) is handed a database id. This side is not:
it searches the pages shared with the integration for one titled `parent_title`, looks
inside it for a database titled `database_title`, and creates that database with a fixed
set of columns when it is missing. A saved id is checked first and dropped if Notion says
it is archived or gone.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from career_copilot.adapters.sources.notion import (
    API_ROOT,
    KEY_ENV,
    NOTION_VERSION,
    TIMEOUT,
    NotionError,
)
from career_copilot.ports import ApplicationEntry, NotionResult

STATUS_OPTIONS: tuple[str, ...] = ("Applied", "Interviewing", "Offer", "Rejected", "Withdrawn")

# Notion caps one rich-text object at 2000 characters; the margin leaves room for the
# ellipsis and for any multi-unit characters Notion counts differently.
WHY_LIMIT = 1900

# Exactly the columns of the spec, in its order. Changing one is a migration in Notion.
COLUMNS: dict[str, dict[str, Any]] = {
    "Role": {"title": {}},
    "Index": {"number": {}},
    "Company": {"rich_text": {}},
    "Source": {"rich_text": {}},
    "Pilot score": {"number": {}},
    "Why": {"rich_text": {}},
    "Date": {"date": {}},
    "Comments": {"rich_text": {}},
    "Status": {"select": {"options": [{"name": name} for name in STATUS_OPTIONS]}},
    "Job URL": {"url": {}},
    "Location": {"rich_text": {}},
}


class NotionPageMissing(NotionError):
    """No page with the parent title is shared with the integration."""


class NotionCannotWrite(NotionError):
    """The key is valid for reading at most: the integration lacks write capabilities."""


CANNOT_WRITE = (
    "The Notion key cannot write. In the integration's settings, tick "
    "Insert content and Update content, then try again."
)


class NotionApplicationLog:
    """One instance per request. Never retries more than once, never runs calls in parallel."""

    def __init__(
        self,
        api_key: str | None = None,
        parent_title: str = "Career Copilot",
        database_title: str = "Applications",
        database_id: str | None = None,
        page_id: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        api_root: str = API_ROOT,
    ) -> None:
        self.api_key = api_key or os.environ.get(KEY_ENV, "")
        self.parent_title = parent_title
        self.database_title = database_title
        self._database_id = database_id
        self._page_id = page_id or ""
        self._client = client or httpx.Client()
        self._sleep = sleep
        self._api_root = api_root

    def record(self, entry: ApplicationEntry) -> NotionResult:
        database_id, page_id = self._database()
        existing = self._row_for_url(database_id, entry.job_url) if entry.job_url else None
        if existing:
            return NotionResult(existing, database_id, page_id, created=False)
        created = self._send(
            "POST",
            "/pages",
            {"parent": {"database_id": database_id}, "properties": _properties(entry)},
        )
        return NotionResult(str(created["id"]), database_id, page_id, created=True)

    # --- finding the database ----------------------------------------------

    def _database(self) -> tuple[str, str]:
        if self._database_id:
            found = self._send("GET", f"/databases/{self._database_id}", missing_ok=True)
            if found is not None and not (found.get("archived") or found.get("in_trash")):
                parent = found.get("parent") or {}
                return self._database_id, str(parent.get("page_id") or self._page_id)
        page_id = self._parent_page()
        database_id = self._child_database(page_id) or self._create_database(page_id)
        return database_id, page_id

    def _parent_page(self) -> str:
        found = self._send(
            "POST",
            "/search",
            {"query": self.parent_title, "filter": {"property": "object", "value": "page"}},
        )
        wanted = self.parent_title.strip().casefold()
        for page in found.get("results") or []:
            if not isinstance(page, dict) or page.get("archived") or page.get("in_trash"):
                continue
            if _page_title(page).strip().casefold() == wanted:
                return str(page["id"])
        raise NotionPageMissing(
            f"Share a page titled '{self.parent_title}' with your integration in Notion "
            "(open the page, then ... > Connections > add the integration)."
        )

    def _child_database(self, page_id: str) -> str | None:
        cursor: str | None = None
        while True:
            query = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            found = self._send("GET", f"/blocks/{page_id}/children{query}")
            for block in found.get("results") or []:
                if not isinstance(block, dict) or block.get("type") != "child_database":
                    continue
                title = str((block.get("child_database") or {}).get("title") or "")
                if title.strip().casefold() == self.database_title.strip().casefold():
                    return str(block["id"])
            cursor = found.get("next_cursor")
            if not found.get("has_more") or not cursor:
                return None

    def _create_database(self, page_id: str) -> str:
        created = self._send(
            "POST",
            "/databases",
            {
                "parent": {"type": "page_id", "page_id": page_id},
                "title": _text(self.database_title),
                "properties": COLUMNS,
            },
        )
        return str(created["id"])

    def _row_for_url(self, database_id: str, url: str) -> str | None:
        found = self._send(
            "POST",
            f"/databases/{database_id}/query",
            {"filter": {"property": "Job URL", "url": {"equals": url}}, "page_size": 1},
        )
        rows = found.get("results") or []
        return str(rows[0]["id"]) if rows and isinstance(rows[0], dict) else None

    # --- transport -----------------------------------------------------------

    def _send(
        self, method: str, path: str, body: dict[str, Any] | None = None, missing_ok: bool = False
    ) -> Any:
        """One Notion call. A 429 waits `Retry-After` once and retries; a second fails."""
        for attempt in range(2):
            try:
                response = self._client.request(
                    method,
                    f"{self._api_root}{path}",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Notion-Version": NOTION_VERSION,
                    },
                    json=body,
                    timeout=TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise NotionError(f"Notion unreachable ({type(exc).__name__})") from None
            if response.status_code == 429 and attempt == 0:
                self._sleep(_retry_after(response))
                continue
            break
        status = response.status_code
        if status == 429:
            raise NotionError("Notion answered with too many requests. Try again in a minute.")
        if status in (401, 403):
            raise NotionCannotWrite(CANNOT_WRITE)
        if status == 404 and missing_ok:
            return None
        if status >= 400:
            raise NotionError(f"Notion returned HTTP {status}. {_message(response)}".strip())
        try:
            payload = response.json()
        except ValueError:
            raise NotionError("Notion returned a response that was not JSON") from None
        if not isinstance(payload, dict):
            raise NotionError("Notion returned a response that was not an object")
        return payload


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", "")).strip()
    except (ValueError, AttributeError):
        return ""


def _page_title(page: dict[str, Any]) -> str:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return "".join(
                str(part.get("plain_text") or "")
                for part in prop.get("title") or []
                if isinstance(part, dict)
            )
    return ""


def _cut(value: str, limit: int = WHY_LIMIT) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": _cut(value)}}] if value else []


def _properties(entry: ApplicationEntry) -> dict[str, Any]:
    return {
        "Role": {"title": _text(entry.role)},
        "Index": {"number": entry.index},
        "Company": {"rich_text": _text(entry.company)},
        "Source": {"rich_text": _text(entry.source)},
        "Pilot score": {"number": entry.pilot_score},
        "Why": {"rich_text": _text(entry.why)},
        "Date": {"date": {"start": entry.applied_on.isoformat()}},
        "Comments": {"rich_text": _text(entry.comments)},
        "Status": {"select": {"name": entry.status}},
        "Job URL": {"url": entry.job_url or None},
        "Location": {"rich_text": _text(entry.location)},
    }
