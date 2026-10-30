"""The federal job board adapter against recorded payloads. No network in CI, ever."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_copilot.adapters.sources.ats import _MIN_INTERVAL_SECONDS, http_fetcher
from career_copilot.adapters.sources.jobsuche import (
    DETAIL_BASE,
    SEARCH_BASE,
    SEARCH_SOURCES,
    JobsucheSource,
    query_for,
)
from career_copilot.domain.models import SourceTarget

FIXTURES = Path(__file__).parent.parent / "fixtures"
SEARCH = json.loads((FIXTURES / "jobsuche_search.json").read_text())
DETAIL = json.loads((FIXTURES / "jobsuche_detail.json").read_text())
TARGET = SourceTarget(source="jobsuche", identifier="was=Python&wo=Berlin&umkreis=25")


def test_a_job_maps_to_a_raw_job() -> None:
    job = JobsucheSource(fetcher=lambda url: {}).parse(SEARCH["ergebnisliste"][0], DETAIL)
    assert job.source == "jobsuche"
    assert job.source_job_id == "12336-a26f539j0456738-S"
    assert job.company == "BWI GmbH"
    assert job.title.startswith("Senior Python Developer")
    assert job.location == "Berlin, DEUTSCHLAND"
    # The agency's own page, not the employer's tracking link.
    assert job.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/12336-a26f539j0456738-S"
    assert "Senior Python Developer" in job.description
    assert job.posted_at is not None and job.posted_at.year == 2026
    assert job.employment_type == "full-time"
    assert job.remote is None


def test_fetch_asks_for_each_listed_job_once_and_returns_them_all() -> None:
    asked: list[str] = []

    def fetch(url: str) -> Any:
        asked.append(url)
        return SEARCH if "/jobs?" in url else DETAIL

    jobs = list(JobsucheSource(fetcher=fetch).fetch(TARGET))
    assert len(jobs) == len(SEARCH["ergebnisliste"]) == 3
    assert len(asked) == 1 + 3
    # Search is on v6 and details on v4: v4 answers 403 for the search.
    assert asked[0].startswith(f"{SEARCH_BASE}/jobs?") and "/pc/v6/jobs?" in asked[0]
    assert asked[1].startswith(f"{DETAIL_BASE}/jobdetails/")
    assert "was=Python" in asked[0] and "wo=Berlin" in asked[0] and "umkreis=25" in asked[0]


def test_one_failing_detail_does_not_lose_the_others() -> None:
    calls = {"detail": 0}

    def fetch(url: str) -> Any:
        if "/jobs?" in url:
            return SEARCH
        calls["detail"] += 1
        if calls["detail"] == 2:
            raise httpx.ReadTimeout("slow")
        return DETAIL

    assert len(list(JobsucheSource(fetcher=fetch).fetch(TARGET))) == 2


def test_a_failed_search_raises_so_the_run_records_a_failure() -> None:
    def fetch(url: str) -> Any:
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        list(JobsucheSource(fetcher=fetch).fetch(TARGET))


def test_an_empty_search_is_not_an_error() -> None:
    assert list(JobsucheSource(fetcher=lambda url: {"ergebnisliste": None}).fetch(TARGET)) == []


def test_only_known_filters_are_passed_and_a_window_is_always_set() -> None:
    query = query_for("was=Go&wo=Köln&evil=1&veroeffentlichtseit=3")
    assert query == {"was": "Go", "wo": "Köln", "veroeffentlichtseit": "3"}
    assert query_for("was=Go")["veroeffentlichtseit"] == "14"


def test_the_detail_path_is_the_base64_of_the_reference() -> None:
    url = JobsucheSource(fetcher=lambda u: {}).detail_url("12336-a26f539j0456738-S")
    assert url == f"{DETAIL_BASE}/jobdetails/MTIzMzYtYTI2ZjUzOWowNDU2NzM4LVM="


def test_the_source_is_registered_apart_from_company_boards() -> None:
    from career_copilot.adapters.sources.ats import SOURCE_TYPES

    assert "jobsuche" in SEARCH_SOURCES and "jobsuche" not in SOURCE_TYPES


def test_the_host_is_rate_limited_and_the_key_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _MIN_INTERVAL_SECONDS["rest.arbeitsagentur.de"] >= 1.0
    seen: dict[str, Any] = {}

    def get(url: str, **kwargs: Any) -> httpx.Response:
        seen.update(kwargs)
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    http_fetcher(headers={"X-API-Key": "k"})("https://rest.arbeitsagentur.de/x")
    assert seen["headers"] == {"X-API-Key": "k"}
