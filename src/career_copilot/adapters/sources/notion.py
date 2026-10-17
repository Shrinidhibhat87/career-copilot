"""`CompanyListProvider` over a Notion database.

Reads the database generically: property *names* are user-defined per database, so
hardcoding "Company"/"URL" would overfit one person's schema. Every Notion database has
exactly one `title` property — that is the company name. The first `url`-typed property,
if there is one, is the board/homepage hint. Everything else present in the row (an
"industry" column, tags, a status select) is never read.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

from career_copilot.domain.company_resolution import extract_ats_hint
from career_copilot.domain.sources import CompanyRef, normalize_company_name

API_ROOT = "https://api.notion.com/v1"
KEY_ENV = "NOTION_API_KEY"
TIMEOUT = 30.0
PAGE_SIZE = 100  # Notion's documented maximum per query.

# Pinned deliberately. Notion's `2025-09-03` version moved row queries to
# /v1/data_sources/{id}/query and `database_id` stopped addressing rows at all, so an
# unpinned request would silently change this adapter's contract.
NOTION_VERSION = "2022-06-28"


class NotionCompanyList:
    """One instance per sync. Paginates the whole database before resolving anything."""

    def __init__(
        self,
        database_id: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        api_root: str = API_ROOT,
    ) -> None:
        self.database_id = database_id
        self.api_key = api_key or os.environ.get(KEY_ENV, "")
        self._client = client or httpx.Client()
        self._api_root = api_root

    def fetch(self) -> Sequence[CompanyRef]:
        refs: list[CompanyRef] = []
        for page in self._pages():
            ref = _to_company_ref(page)
            if ref is not None:
                refs.append(ref)
        return refs

    def _pages(self) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            payload = self._query(cursor)
            for page in payload.get("results") or []:
                if isinstance(page, dict):
                    yield page
            if not payload.get("has_more"):
                return
            cursor = payload.get("next_cursor")
            if not cursor:
                # `has_more` without a cursor would otherwise re-request page one
                # forever; treat the contradiction as the end of the list.
                return

    def _query(self, cursor: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": PAGE_SIZE}
        if cursor:
            body["start_cursor"] = cursor
        try:
            response = self._client.post(
                f"{self._api_root}/databases/{self.database_id}/query",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Notion-Version": NOTION_VERSION,
                },
                json=body,
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise NotionError(_explain(exc)) from None
        except httpx.HTTPError as exc:
            raise NotionError(f"Notion unreachable ({type(exc).__name__})") from None
        except ValueError:
            raise NotionError("Notion returned a response that was not JSON") from None
        if not isinstance(payload, dict):
            raise NotionError("Notion returned a response that was not an object")
        return payload


class NotionError(RuntimeError):
    """A Notion request failed. The message is meant to be shown to the user as-is."""


def _explain(exc: httpx.HTTPStatusError) -> str:
    """Notion's own `message` is more useful than the status code, and its two most
    common failures are both configuration mistakes worth naming explicitly."""
    status = exc.response.status_code
    try:
        detail = str(exc.response.json().get("message", "")).strip()
    except ValueError:
        detail = ""
    if status == 401:
        return f"Notion rejected {KEY_ENV} (401). {detail}".strip()
    if status == 404:
        return (
            f"Notion cannot see database {_short(exc.request.url.path)} (404). Either the id "
            "is wrong, or the database has not been shared with your integration — "
            f"open it in Notion, then ... > Connections > add the integration. {detail}"
        ).strip()
    return f"Notion returned HTTP {status}. {detail}".strip()


def _short(path: str) -> str:
    return path.rsplit("/", 2)[-2] if "/" in path else path


def _to_company_ref(page: dict[str, Any]) -> CompanyRef | None:
    """A row with no title text is a blank line in the user's database, not an error."""
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None
    name = normalize_company_name(_title_of(properties))
    if not name:
        return None
    external_id = str(page.get("id") or "")
    if not external_id:
        return None
    return CompanyRef(external_id=external_id, name=name, url=_url_of(properties))


def _title_of(properties: dict[str, Any]) -> str:
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return "".join(
                str(part.get("plain_text") or "")
                for part in prop.get("title") or []
                if isinstance(part, dict)
            )
    return ""


def _url_of(properties: dict[str, Any]) -> str | None:
    """Every `url`-type column is a candidate (a database may hold a website and a careers
    link side by side); one that already names an ATS board wins, else the first one."""
    urls = [
        str(prop.get("url") or "").strip()
        for prop in properties.values()
        if isinstance(prop, dict) and prop.get("type") == "url"
    ]
    urls = [url for url in urls if url]
    for url in urls:
        if extract_ats_hint(url):
            return url
    return urls[0] if urls else None
