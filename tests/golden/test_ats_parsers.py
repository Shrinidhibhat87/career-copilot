"""Parser tests against recorded payloads. No network in CI, ever."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_copilot.adapters.sources.ats import (
    BoardNotFound,
    GreenhouseSource,
    HostThrottle,
    LeverSource,
    PersonioSource,
    RecruiteeSource,
    SmartRecruitersSource,
    TeamtailorSource,
    WorkableSource,
    http_fetcher,
    strip_html,
)
from career_copilot.domain.models import SourceTarget

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def fetcher_for(name: str) -> Any:
    def fetch(url: str) -> Any:
        if name.endswith(".xml"):
            return (FIXTURES / name).read_text()
        return load(name)

    return fetch


def test_strip_html_unwraps_tags_and_entities() -> None:
    assert strip_html("<p>Work &amp; learn</p>") == "Work & learn"


def test_greenhouse_maps_every_job() -> None:
    source = GreenhouseSource(fetcher_for("greenhouse_board.json"))
    jobs = list(source.fetch(SourceTarget("greenhouse", "acme")))
    assert len(jobs) == 2
    first = jobs[0]
    assert first.source_job_id == "4512345"
    assert first.company == "Acme GmbH"
    assert first.title == "Forward Deployed Engineer (m/w/d)"
    assert first.location == "Berlin, Germany"
    assert "<" not in first.description
    assert first.posted_at is not None


def test_greenhouse_url_shape() -> None:
    assert GreenhouseSource(fetcher_for("greenhouse_board.json")).url_for("acme") == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    )


def test_lever_maps_commitment_and_remote_flag() -> None:
    jobs = list(LeverSource(fetcher_for("lever_board.json")).fetch(SourceTarget("lever", "beta")))
    assert len(jobs) == 2
    assert jobs[0].employment_type == "Full-time"
    assert jobs[0].remote is None
    assert jobs[1].remote is True


def test_lever_falls_back_to_html_description() -> None:
    jobs = list(LeverSource(fetcher_for("lever_board.json")).fetch(SourceTarget("lever", "beta")))
    assert jobs[1].description == "Support the data team while studying."


@pytest.mark.parametrize("source_cls", [GreenhouseSource, LeverSource])
def test_empty_board_yields_no_jobs_and_does_not_raise(source_cls: Any) -> None:
    empty: Any = {"jobs": []} if source_cls is GreenhouseSource else []
    jobs = list(source_cls(lambda url: empty).fetch(SourceTarget(source_cls.name, "x")))
    assert jobs == []


def test_personio_maps_positions_and_joins_description_sections() -> None:
    source = PersonioSource(fetcher_for("personio_board.xml"))
    jobs = list(source.fetch(SourceTarget("personio", "acme")))
    assert [job.source_job_id for job in jobs] == ["1001", "1002"]
    first = jobs[0]
    assert first.company == "Acme Robotics GmbH"
    assert first.title == "Senior Data Engineer (m/w/d)"
    assert first.location == "Berlin"
    assert first.url == "https://acme.jobs.personio.de/job/1001"
    assert first.description == "Build pipelines & models for our fleet. 5+ years of Python"
    assert first.employment_type == "full-time"
    assert first.posted_at is not None
    assert jobs[1].company == "acme"
    assert jobs[1].description == ""


def test_personio_html_answer_is_a_missing_board() -> None:
    source = PersonioSource(lambda url: "<!DOCTYPE html><html><body>Page Not Found</body></html>")
    with pytest.raises(BoardNotFound):
        source.fetch(SourceTarget("personio", "nobody"))


def test_teamtailor_maps_items_and_dedupes_locations() -> None:
    source = TeamtailorSource(fetcher_for("teamtailor_board.xml"))
    jobs = list(source.fetch(SourceTarget("teamtailor", "acme")))
    assert [job.source_job_id for job in jobs] == ["111", "112"]
    first = jobs[0]
    assert first.company == "Acme AI"
    assert first.title == "Machine Learning Engineer"
    assert first.location == "Amsterdam, Gand"
    assert first.url == "https://acme.teamtailor.com/jobs/111-machine-learning-engineer"
    assert first.description == "Train models & ship them."
    assert first.remote is True
    assert first.posted_at is not None
    assert jobs[1].remote is None
    assert jobs[1].location == ""


def test_teamtailor_html_answer_is_a_missing_board() -> None:
    with pytest.raises(BoardNotFound):
        TeamtailorSource(lambda url: "<html><body>404</body></html>").fetch(
            SourceTarget("teamtailor", "nobody")
        )


def test_workable_maps_jobs_and_remote_flag() -> None:
    source = WorkableSource(fetcher_for("workable_board.json"))
    jobs = list(source.fetch(SourceTarget("workable", "acme")))
    assert [job.source_job_id for job in jobs] == ["ABC123", "DEF456"]
    first = jobs[0]
    assert first.company == "Acme Labs"
    assert first.location == "Berlin, Germany"
    assert first.url == "https://apply.workable.com/j/ABC123"
    assert first.description == "Own our APIs & data layer."
    assert first.remote is True
    assert jobs[1].remote is None


def test_workable_account_with_no_name_and_no_jobs_is_a_missing_board() -> None:
    with pytest.raises(BoardNotFound):
        WorkableSource(fetcher_for("workable_unknown.json")).fetch(
            SourceTarget("workable", "nobody")
        )


def test_workable_named_account_with_no_jobs_is_an_empty_board() -> None:
    empty: Any = {"name": "Acme Labs", "jobs": []}
    assert list(WorkableSource(lambda url: empty).fetch(SourceTarget("workable", "acme"))) == []


def test_recruitee_maps_offers_and_skips_unpublished_ones() -> None:
    source = RecruiteeSource(fetcher_for("recruitee_board.json"))
    jobs = list(source.fetch(SourceTarget("recruitee", "acme")))
    assert [job.source_job_id for job in jobs] == ["2771667", "2771668"]
    first = jobs[0]
    assert first.company == "Acme Bank"
    assert first.location == "Amsterdam, Noord-Holland, Netherlands"
    assert first.url == "https://careers.acme.example/o/platform-engineer"
    assert first.description == "Run our platform . Kubernetes"
    assert first.posted_at is not None
    assert first.remote is None
    assert jobs[1].remote is True


def test_recruitee_payload_without_offers_is_a_missing_board() -> None:
    with pytest.raises(BoardNotFound):
        RecruiteeSource(lambda url: {"error": "Not Found"}).fetch(SourceTarget("recruitee", "x"))


def _smartrecruiters_fetcher() -> Any:
    postings, detail = load("smartrecruiters_postings.json"), load("smartrecruiters_detail.json")
    calls: list[str] = []

    def fetch(url: str) -> Any:
        calls.append(url)
        if url.endswith("744000000000001"):
            return detail
        if "/postings/" in url:
            raise AssertionError("only the first posting has a recorded detail")
        return postings

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_smartrecruiters_reads_each_postings_detail_for_its_description() -> None:
    fetch = _smartrecruiters_fetcher()
    source = SmartRecruitersSource(lambda url: _detail_or_raise(fetch, url))
    jobs = list(source.fetch(SourceTarget("smartrecruiters", "AcmeLab")))
    assert [job.source_job_id for job in jobs] == ["744000000000001", "744000000000002"]
    first, second = jobs
    assert first.company == "Acme Lab"
    assert first.location == "Geneva, Switzerland"
    assert first.url.endswith("744000000000001-research-engineer")
    assert first.description == "We study physics . Build detectors & software. MSc"
    assert first.employment_type == "Full-time"
    # A posting whose detail could not be read is kept, with no description.
    assert second.description == ""
    assert second.url == "https://jobs.smartrecruiters.com/AcmeLab/744000000000002"
    assert second.remote is True


def _detail_or_raise(fetch: Any, url: str) -> Any:
    try:
        return fetch(url)
    except AssertionError:
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(500, request=request)
        ) from None


def test_smartrecruiters_unknown_company_is_a_missing_board_for_the_probe() -> None:
    source = SmartRecruitersSource(fetcher_for("smartrecruiters_unknown.json"))
    with pytest.raises(BoardNotFound):
        list(source.parse(load("smartrecruiters_unknown.json"), "nobody"))


def test_smartrecruiters_crawl_of_a_board_with_no_postings_is_empty_not_an_error() -> None:
    source = SmartRecruitersSource(fetcher_for("smartrecruiters_unknown.json"))
    assert list(source.fetch(SourceTarget("smartrecruiters", "nobody"))) == []


def test_host_throttle_spaces_requests_to_a_limited_host_only() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = HostThrottle({"slow.example": 0.5}, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        throttle.wait("slow.example")
    throttle.wait("fast.example")
    assert slept == [0.5, 0.5]


def test_host_throttle_block_lasts_for_the_time_asked_and_only_for_that_host() -> None:
    now = [100.0]
    throttle = HostThrottle({}, clock=lambda: now[0])
    assert throttle.blocked_for("a.example") == 0.0
    throttle.block("a.example", 60.0)
    assert throttle.blocked_for("a.example") == pytest.approx(60.0)
    assert throttle.blocked_for("b.example") == 0.0
    now[0] += 61.0
    assert throttle.blocked_for("a.example") == 0.0


def test_a_long_429_stops_every_further_request_to_that_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        sent.append(url)
        request = httpx.Request("GET", url)
        return httpx.Response(429, headers={"retry-after": "86400"}, request=request)

    monkeypatch.setattr(httpx, "get", get)
    fetch = http_fetcher(throttle=HostThrottle({}))
    for slug in ("one", "two", "three"):
        with pytest.raises(httpx.HTTPStatusError) as raised:
            fetch(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
        assert raised.value.response.status_code == 429
    assert sent == ["https://apply.workable.com/api/v1/widget/accounts/one"]
    # Another host is unaffected.
    with pytest.raises(httpx.HTTPStatusError):
        fetch("https://api.lever.co/v0/postings/x")
    assert len(sent) == 2


def test_a_short_429_is_retried_and_does_not_block_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [429, 200]
    sent: list[str] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        sent.append(url)
        request = httpx.Request("GET", url)
        status = answers.pop(0)
        return httpx.Response(
            status, headers={"retry-after": "0"}, json={"jobs": []}, request=request
        )

    monkeypatch.setattr(httpx, "get", get)
    throttle = HostThrottle({})
    assert http_fetcher(throttle=throttle)("https://boards-api.greenhouse.io/x") == {"jobs": []}
    assert len(sent) == 2
    assert throttle.blocked_for("boards-api.greenhouse.io") == 0.0
