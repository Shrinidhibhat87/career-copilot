"""The Notion adapter against recorded responses. No live network: the fixtures carry
the two shapes that matter — a paginated query, and a property set holding a title, a
url and an ignored "industry" column.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_copilot.adapters.sources.notion import (
    NOTION_VERSION,
    NotionCompanyList,
    NotionError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return payload


def client(*pages: dict[str, Any], record: list[httpx.Request] | None = None) -> httpx.Client:
    """One recorded response per request, in order."""
    remaining = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(200, json=remaining.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler))


def failing(status: int, body: dict[str, Any] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body or {})

    return httpx.Client(transport=httpx.MockTransport(handler))


def provider(http: httpx.Client) -> NotionCompanyList:
    return NotionCompanyList("db-1", api_key="secret", client=http)


def test_it_reads_the_title_property_whatever_it_is_called() -> None:
    refs = provider(client(load("notion_companies_page2.json"))).fetch()
    assert [ref.name for ref in refs] == ["Title Only AG"]


def test_a_title_split_across_rich_text_parts_is_rejoined() -> None:
    refs = provider(
        client(load("notion_companies_page1.json"), load("notion_companies_empty.json"))
    ).fetch()
    # The fixture splits the title into "Acme " and "GmbH"; normalization collapses the gap.
    assert "Acme GmbH" in [ref.name for ref in refs]


def test_the_url_property_is_read_and_other_properties_are_ignored() -> None:
    refs = provider(
        client(load("notion_companies_page1.json"), load("notion_companies_empty.json"))
    ).fetch()
    acme = next(ref for ref in refs if ref.name == "Acme GmbH")
    assert acme.url == "https://boards.greenhouse.io/acme"
    # "Industry" and "Notes" are present in the fixture and must not surface anywhere.
    assert acme.external_id == "page-acme"


def test_a_null_url_is_none_not_an_empty_string() -> None:
    refs = provider(
        client(load("notion_companies_page1.json"), load("notion_companies_empty.json"))
    ).fetch()
    cern = next(ref for ref in refs if ref.name == "CERN")
    assert cern.url is None


def test_the_name_is_normalized() -> None:
    refs = provider(
        client(load("notion_companies_page1.json"), load("notion_companies_empty.json"))
    ).fetch()
    # `{CERN}` in the fixture: one layer of wrapping braces is stripped.
    assert "CERN" in [ref.name for ref in refs]


def test_it_follows_has_more_to_the_next_cursor() -> None:
    requests: list[httpx.Request] = []
    refs = NotionCompanyList(
        "db-1",
        api_key="secret",
        client=client(
            load("notion_companies_page1.json"),
            load("notion_companies_page2.json"),
            record=requests,
        ),
    ).fetch()
    assert [ref.external_id for ref in refs] == ["page-acme", "page-cern", "page-titleonly"]
    assert len(requests) == 2
    assert json.loads(requests[1].content.decode())["start_cursor"] == "cursor-page-2"


def test_a_row_with_no_title_text_is_skipped_not_an_error() -> None:
    refs = provider(client(load("notion_companies_page2.json"))).fetch()
    assert [ref.external_id for ref in refs] == ["page-titleonly"]


def test_an_empty_database_is_an_empty_list() -> None:
    assert provider(client(load("notion_companies_empty.json"))).fetch() == []


def test_it_sends_the_pinned_api_version_and_the_bearer_token() -> None:
    requests: list[httpx.Request] = []
    NotionCompanyList(
        "db-1",
        api_key="secret",
        client=client(load("notion_companies_empty.json"), record=requests),
    ).fetch()
    assert requests[0].headers["Notion-Version"] == NOTION_VERSION
    assert requests[0].headers["Authorization"] == "Bearer secret"
    assert requests[0].url.path == "/v1/databases/db-1/query"


def test_a_401_names_the_env_var_rather_than_the_status_code() -> None:
    with pytest.raises(NotionError) as caught:
        provider(failing(401, {"message": "API token is invalid."})).fetch()
    assert "NOTION_API_KEY" in str(caught.value)


def test_a_404_explains_that_the_database_may_not_be_shared() -> None:
    with pytest.raises(NotionError) as caught:
        provider(failing(404, {"message": "Could not find database."})).fetch()
    assert "shared with your integration" in str(caught.value)


def test_a_transport_failure_is_a_notion_error_not_an_httpx_one() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    http = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(NotionError) as caught:
        provider(http).fetch()
    assert "unreachable" in str(caught.value)


def test_has_more_without_a_cursor_terminates_instead_of_looping() -> None:
    contradiction = {"results": [], "has_more": True, "next_cursor": None}
    assert provider(client(contradiction)).fetch() == []


def _row(**props: dict[str, Any]) -> dict[str, Any]:
    title = {"type": "title", "title": [{"plain_text": "Peec AI"}]}
    return {
        "results": [{"id": "p1", "properties": {"Name": title, **props}}],
        "has_more": False,
        "next_cursor": None,
    }


def test_an_ats_url_in_a_later_url_column_wins_over_the_first_url() -> None:
    page = _row(
        Website={"type": "url", "url": "https://peec.ai"},
        Careers={"type": "url", "url": "https://jobs.ashbyhq.com/Peec"},
    )
    (ref,) = provider(client(page)).fetch()
    assert ref.url == "https://jobs.ashbyhq.com/Peec"


def test_without_an_ats_url_the_first_url_column_is_kept() -> None:
    page = _row(
        Website={"type": "url", "url": "https://peec.ai"},
        Other={"type": "url", "url": "https://example.com/x"},
    )
    (ref,) = provider(client(page)).fetch()
    assert ref.url == "https://peec.ai"
