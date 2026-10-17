"""The Notion Applications writer, against a fake Notion served by `httpx.MockTransport`."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest

from career_copilot.adapters.notion_applications import (
    STATUS_OPTIONS,
    WHY_LIMIT,
    NotionApplicationLog,
    NotionCannotWrite,
    NotionPageMissing,
)
from career_copilot.adapters.sources.notion import NotionError
from career_copilot.ports import ApplicationEntry

PAGE = {
    "object": "page",
    "id": "parent-1",
    "properties": {"title": {"type": "title", "title": [{"plain_text": "Career Copilot"}]}},
}


def entry(**overrides: Any) -> ApplicationEntry:
    fields: dict[str, Any] = {
        "index": 1,
        "role": "Data Engineer",
        "company": "Example GmbH",
        "source": "greenhouse",
        "pilot_score": 82,
        "why": "Owns the pipeline.",
        "applied_on": date(2026, 10, 8),
        "comments": "",
        "status": "Applied",
        "job_url": "https://x.test/1",
        "location": "Berlin",
        **overrides,
    }
    return ApplicationEntry(**fields)


class FakeNotion:
    """Records every request; answers from `routes`, a `(method, path) -> handler` map."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {
            ("POST", "/v1/search"): lambda _: httpx.Response(200, json={"results": [PAGE]}),
            ("GET", "/v1/blocks/parent-1/children"): lambda _: httpx.Response(
                200, json={"results": [], "has_more": False}
            ),
            ("POST", "/v1/databases"): lambda _: httpx.Response(200, json={"id": "db-new"}),
            ("POST", "/v1/databases/db-new/query"): lambda _: httpx.Response(
                200, json={"results": []}
            ),
            ("POST", "/v1/pages"): lambda _: httpx.Response(200, json={"id": "row-1"}),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        return self.routes[(request.method, request.url.path)](request)

    def bodies(self, method: str, path: str) -> list[dict[str, Any]]:
        return [b or {} for m, p, b in self.calls if (m, p) == (method, path)]


def log(fake: FakeNotion, database_id: str | None = None, **kw: Any) -> NotionApplicationLog:
    return NotionApplicationLog(
        api_key="secret",
        database_id=database_id,
        client=httpx.Client(transport=httpx.MockTransport(fake)),
        **kw,
    )


def test_with_no_database_it_creates_one_with_exactly_the_columns_then_adds_the_row() -> None:
    fake = FakeNotion()
    result = log(fake).record(entry())

    [created] = fake.bodies("POST", "/v1/databases")
    assert created["parent"] == {"type": "page_id", "page_id": "parent-1"}
    assert created["title"][0]["text"]["content"] == "Applications"
    columns = created["properties"]
    assert {name: next(iter(spec)) for name, spec in columns.items()} == {
        "Role": "title",
        "Index": "number",
        "Company": "rich_text",
        "Source": "rich_text",
        "Pilot score": "number",
        "Why": "rich_text",
        "Date": "date",
        "Comments": "rich_text",
        "Status": "select",
        "Job URL": "url",
        "Location": "rich_text",
    }
    assert [o["name"] for o in columns["Status"]["select"]["options"]] == list(STATUS_OPTIONS)
    assert STATUS_OPTIONS == ("Applied", "Interviewing", "Offer", "Rejected", "Withdrawn")

    [row] = fake.bodies("POST", "/v1/pages")
    assert row["parent"] == {"database_id": "db-new"}
    props = row["properties"]
    assert props["Role"]["title"][0]["text"]["content"] == "Data Engineer"
    assert props["Index"] == {"number": 1}
    assert props["Pilot score"] == {"number": 82}
    assert props["Date"] == {"date": {"start": "2026-10-08"}}
    assert props["Status"] == {"select": {"name": "Applied"}}
    assert props["Job URL"] == {"url": "https://x.test/1"}
    assert (result.page_id, result.database_id, result.parent_page_id, result.created) == (
        "row-1",
        "db-new",
        "parent-1",
        True,
    )


def test_an_existing_database_in_the_page_is_reused_not_created() -> None:
    fake = FakeNotion()
    fake.routes[("GET", "/v1/blocks/parent-1/children")] = lambda _: httpx.Response(
        200,
        json={
            "results": [
                {"id": "other", "type": "child_database", "child_database": {"title": "Notes"}},
                {
                    "id": "db-new",
                    "type": "child_database",
                    "child_database": {"title": "Applications"},
                },
            ],
            "has_more": False,
        },
    )
    log(fake).record(entry())
    assert fake.bodies("POST", "/v1/databases") == []
    assert len(fake.bodies("POST", "/v1/pages")) == 1


def test_a_saved_database_that_is_archived_triggers_a_new_search_and_create() -> None:
    fake = FakeNotion()
    fake.routes[("GET", "/v1/databases/db-old")] = lambda _: httpx.Response(
        200, json={"id": "db-old", "archived": True}
    )
    result = log(fake, database_id="db-old").record(entry())
    assert len(fake.bodies("POST", "/v1/search")) == 1
    assert len(fake.bodies("POST", "/v1/databases")) == 1
    assert result.database_id == "db-new"


def test_a_saved_database_that_is_missing_triggers_a_new_search_and_create() -> None:
    fake = FakeNotion()
    fake.routes[("GET", "/v1/databases/db-old")] = lambda _: httpx.Response(
        404, json={"message": "Could not find database"}
    )
    assert log(fake, database_id="db-old").record(entry()).database_id == "db-new"


def test_a_live_saved_database_is_used_without_searching() -> None:
    fake = FakeNotion()
    fake.routes[("GET", "/v1/databases/db-new")] = lambda _: httpx.Response(
        200, json={"id": "db-new", "archived": False, "parent": {"page_id": "parent-1"}}
    )
    log(fake, database_id="db-new").record(entry())
    assert fake.bodies("POST", "/v1/search") == []
    assert fake.bodies("POST", "/v1/databases") == []


def test_a_row_already_in_notion_for_the_job_url_is_not_added_twice() -> None:
    fake = FakeNotion()
    fake.routes[("POST", "/v1/databases/db-new/query")] = lambda _: httpx.Response(
        200, json={"results": [{"id": "row-old"}]}
    )
    result = log(fake).record(entry())
    [query] = fake.bodies("POST", "/v1/databases/db-new/query")
    assert query["filter"] == {"property": "Job URL", "url": {"equals": "https://x.test/1"}}
    assert fake.bodies("POST", "/v1/pages") == []
    assert (result.page_id, result.created) == ("row-old", False)


def test_no_shared_parent_page_says_so() -> None:
    fake = FakeNotion()
    fake.routes[("POST", "/v1/search")] = lambda _: httpx.Response(200, json={"results": []})
    with pytest.raises(NotionPageMissing, match="Share a page titled 'Career Copilot'"):
        log(fake).record(entry())


def test_429_then_200_succeeds_after_one_wait() -> None:
    fake = FakeNotion()
    answers = iter(
        [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"id": "r"})]
    )
    fake.routes[("POST", "/v1/pages")] = lambda _: next(answers)
    waits: list[float] = []
    result = log(fake, sleep=waits.append).record(entry())
    assert waits == [2.0]
    assert result.page_id == "r"


def test_429_twice_fails_with_a_message() -> None:
    fake = FakeNotion()
    fake.routes[("POST", "/v1/pages")] = lambda _: httpx.Response(429, headers={"Retry-After": "1"})
    waits: list[float] = []
    with pytest.raises(NotionError, match="too many requests"):
        log(fake, sleep=waits.append).record(entry())
    assert waits == [1.0]


def test_401_returns_the_write_permission_message() -> None:
    fake = FakeNotion()
    fake.routes[("POST", "/v1/pages")] = lambda _: httpx.Response(401, json={"message": "no"})
    with pytest.raises(NotionCannotWrite, match="tick Insert content"):
        log(fake).record(entry())


def test_a_why_of_5000_characters_is_sent_as_at_most_1900() -> None:
    fake = FakeNotion()
    log(fake).record(entry(why="x" * 5000))
    [row] = fake.bodies("POST", "/v1/pages")
    sent = "".join(part["text"]["content"] for part in row["properties"]["Why"]["rich_text"])
    assert WHY_LIMIT == 1900
    assert len(sent) <= 1900


def test_every_request_carries_the_key_and_pinned_version() -> None:
    seen: list[httpx.Request] = []
    fake = FakeNotion()

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return fake(request)

    NotionApplicationLog(
        api_key="secret", client=httpx.Client(transport=httpx.MockTransport(spy))
    ).record(entry())
    assert {r.headers["Authorization"] for r in seen} == {"Bearer secret"}
    assert {r.headers["Notion-Version"] for r in seen} == {"2022-06-28"}
