"""Resolution is deterministic slug-guessing against real ATS APIs. No LLM, no search —
a clean 404 and a blocked probe must be told apart, since they mean different things.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_copilot.domain.company_resolution import (
    domain_label,
    extract_ats_hint,
    resolve_company,
    slug_candidates,
)
from career_copilot.domain.sources import CompanyRef, ImportStatus

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BOARD = json.loads((FIXTURES / "greenhouse_board.json").read_text())


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def test_extract_ats_hint_reads_a_known_board_url() -> None:
    assert extract_ats_hint("https://boards.greenhouse.io/acme/jobs/1") == ("greenhouse", "acme")
    assert extract_ats_hint("https://jobs.lever.co/acme") == ("lever", "acme")
    assert extract_ats_hint("https://acme.example.com") is None
    assert extract_ats_hint(None) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://job-boards.eu.greenhouse.io/arxroboticsgmbh/jobs/4875205101",
        "https://job-boards.greenhouse.io/arxroboticsgmbh",
        "https://boards.eu.greenhouse.io/arxroboticsgmbh",
        "https://boards.greenhouse.io/embed/job_board?for=arxroboticsgmbh",
    ],
)
def test_every_greenhouse_host_yields_the_slug(url: str) -> None:
    assert extract_ats_hint(url) == ("greenhouse", "arxroboticsgmbh")


@pytest.mark.parametrize(
    ("url", "hint"),
    [
        ("https://merantix.jobs.personio.de/job/123", ("personio", "merantix")),
        ("https://acme.jobs.personio.com/", ("personio", "acme")),
        ("https://apply.workable.com/acme/j/ABC123/", ("workable", "acme")),
        ("https://acme.workable.com/", ("workable", "acme")),
        ("https://ml6.teamtailor.com/jobs", ("teamtailor", "ml6")),
        ("https://jobs.smartrecruiters.com/CERN/744000", ("smartrecruiters", "CERN")),
        ("https://bunq.recruitee.com/o/website-lead", ("recruitee", "bunq")),
    ],
)
def test_every_vendors_careers_url_yields_the_slug(url: str, hint: tuple[str, str]) -> None:
    assert extract_ats_hint(url) == hint


@pytest.mark.parametrize(
    "url",
    [
        "https://www.recruitee.com/pricing",
        "https://www.teamtailor.com/en/",
        "https://apply.workable.com/api/v1/x",
    ],
)
def test_a_vendors_own_marketing_pages_are_not_boards(url: str) -> None:
    assert extract_ats_hint(url) is None


def test_ashby_hint_keeps_the_slug_as_written() -> None:
    assert extract_ats_hint("https://jobs.ashbyhq.com/Peec") == ("ashby", "Peec")


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://www.deliverect.com/en/careers", "deliverect"),
        ("https://careers.acme.co.uk/jobs", "acme"),
        ("https://www.linkedin.com/company/acme", None),
        ("https://boards.greenhouse.io/acme", None),
        (None, None),
    ],
)
def test_domain_label_names_only_a_companys_own_site(url: str | None, label: str | None) -> None:
    assert domain_label(url) == label


def test_slug_candidates_are_generic_hygiene_not_personalization() -> None:
    assert slug_candidates("{CERN}") == ("cern", "cerngmbh", "cern-gmbh")
    # The name as written comes first; the "&" as "and" spellings follow it.
    assert slug_candidates("Acme & Co.")[:4] == (
        "acme-co",
        "acmeco",
        "acmecogmbh",
        "acme-co-gmbh",
    )
    assert "acme-and-co" in slug_candidates("Acme & Co.")


def test_trailing_descriptors_are_peeled_off_one_at_a_time() -> None:
    assert slug_candidates("Peec AI") == (
        "peec-ai",
        "peecai",
        "peec",
        "peecaigmbh",
        "peec-ai-gmbh",
    )
    assert slug_candidates("ARX Robotics") == (
        "arx-robotics",
        "arxrobotics",
        "arx",
        "arxroboticsgmbh",
        "arx-robotics-gmbh",
    )
    assert slug_candidates("fore.ai")[:3] == ("fore-ai", "foreai", "fore")


def test_a_hyphenated_gmbh_board_name_is_guessed_too() -> None:
    """ZeroG's Personio board is `zerog-gmbh`."""
    assert "zerog-gmbh" in slug_candidates("ZeroG")


def test_a_legal_form_already_in_the_name_is_not_appended_again() -> None:
    assert "acmegmbhgmbh" not in slug_candidates("Acme GmbH")


def test_the_company_domain_is_a_fallback_candidate() -> None:
    candidates = slug_candidates("Totally Unrelated", "https://www.deliverect.com/en/careers")
    assert "deliverect" in candidates


def test_a_url_hint_that_verifies_resolves_without_guessing() -> None:
    def fetch(url: str) -> Any:
        assert "acme" in url
        return BOARD

    ref = CompanyRef("1", "Acme GmbH", url="https://boards.greenhouse.io/acme")
    result = resolve_company(ref, fetch)
    assert result.status is ImportStatus.RESOLVED
    assert (result.board_type, result.slug) == ("greenhouse", "acme")


def test_a_clean_miss_on_every_candidate_is_unresolved_not_blocked() -> None:
    def fetch(url: str) -> Any:
        raise _http_error(404)

    ref = CompanyRef("1", "Nonexistent Startup")
    result = resolve_company(ref, fetch)
    assert result.status is ImportStatus.UNRESOLVED
    assert "no public board found" in (result.last_error or "")
    assert "nonexistent-startup" in (result.last_error or "")


def test_a_non_404_failure_is_blocked_not_unresolved() -> None:
    def fetch(url: str) -> Any:
        raise httpx.TimeoutException("timed out")

    ref = CompanyRef("1", "Firewalled Corp")
    result = resolve_company(ref, fetch)
    assert result.status is ImportStatus.BLOCKED
    assert "timed out" in (result.last_error or "")


def test_falls_back_to_slug_guessing_when_no_url_hint_is_given() -> None:
    def fetch(url: str) -> Any:
        if "/boards/acme/" in url:
            return BOARD
        raise _http_error(404)

    ref = CompanyRef("1", "Acme")
    result = resolve_company(ref, fetch)
    assert result.status is ImportStatus.RESOLVED
    assert (result.board_type, result.slug) == ("greenhouse", "acme")


def test_a_url_hint_miss_still_falls_back_to_slug_guessing() -> None:
    def fetch(url: str) -> Any:
        if "/boards/acme-gmbh/" in url or "/boards/acmegmbh/" in url:
            return BOARD
        raise _http_error(404)

    ref = CompanyRef("1", "Acme GmbH", url="https://boards.greenhouse.io/wrong-slug")
    result = resolve_company(ref, fetch)
    assert result.status is ImportStatus.RESOLVED


@pytest.mark.parametrize("status", [403, 500])
def test_other_http_errors_are_blocked(status: int) -> None:
    def fetch(url: str) -> Any:
        raise _http_error(status)

    result = resolve_company(CompanyRef("1", "Some Corp"), fetch)
    assert result.status is ImportStatus.BLOCKED
    assert f"HTTP {status}" in (result.last_error or "")


def _board_only(slug: str, board: str = "greenhouse") -> Any:
    def fetch(url: str) -> Any:
        if f"/{slug}" in url and board in url and f"/{slug}-" not in url:
            return BOARD
        raise _http_error(404)

    return fetch


def test_peec_ai_resolves_through_its_stripped_name() -> None:
    def fetch(url: str) -> Any:
        if "/job-board/peec?" in url:
            return {"jobs": []}
        raise _http_error(404)

    result = resolve_company(CompanyRef("1", "Peec AI"), fetch)
    assert result.status is ImportStatus.RESOLVED
    assert (result.board_type, result.slug) == ("ashby", "peec")


def test_arx_robotics_resolves_through_the_legal_form_variant() -> None:
    result = resolve_company(CompanyRef("1", "ARX Robotics"), _board_only("arxroboticsgmbh"))
    assert result.status is ImportStatus.RESOLVED
    assert (result.board_type, result.slug) == ("greenhouse", "arxroboticsgmbh")


def test_an_eu_greenhouse_url_resolves_without_name_guessing() -> None:
    ref = CompanyRef(
        "1", "Totally Different Name", url="https://job-boards.eu.greenhouse.io/arxroboticsgmbh"
    )
    result = resolve_company(ref, _board_only("arxroboticsgmbh"))
    assert (result.status, result.slug) == (ImportStatus.RESOLVED, "arxroboticsgmbh")


def test_a_careers_url_in_the_name_column_is_resolved_via_its_domain() -> None:
    ref = CompanyRef("1", "https://www.deliverect.com/en/careers")
    result = resolve_company(ref, _board_only("deliverect"))
    assert (result.status, result.slug) == (ImportStatus.RESOLVED, "deliverect")
    assert result.ref == ref


def test_a_found_board_reports_how_many_jobs_it_holds() -> None:
    result = resolve_company(CompanyRef("1", "Acme"), _board_only("acme"))
    assert result.status is ImportStatus.RESOLVED
    assert result.open_roles == len(BOARD["jobs"])


def test_a_board_with_no_jobs_is_found_with_zero_roles() -> None:
    def fetch(url: str) -> Any:
        if "/boards/acme/" in url:
            return {"jobs": []}
        raise _http_error(404)

    result = resolve_company(CompanyRef("1", "Acme"), fetch)
    assert (result.status, result.open_roles) == (ImportStatus.RESOLVED, 0)


def test_no_board_means_no_role_count() -> None:
    def fetch(url: str) -> Any:
        raise _http_error(404)

    assert resolve_company(CompanyRef("1", "Nowhere"), fetch).open_roles is None


def test_a_200_with_no_postings_from_smartrecruiters_is_unresolved_not_resolved() -> None:
    """SmartRecruiters answers 200 for any company name; only `totalFound > 0` is a board."""
    unknown = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}

    def fetch(url: str) -> Any:
        if "smartrecruiters" in url:
            return unknown
        raise _http_error(404)

    result = resolve_company(CompanyRef("1", "Nobody Corp"), fetch)
    assert result.status is ImportStatus.UNRESOLVED


def test_a_company_on_a_newer_vendor_resolves_through_slug_guessing() -> None:
    board = json.loads((FIXTURES / "recruitee_board.json").read_text())

    def fetch(url: str) -> Any:
        if url == "https://acme.recruitee.com/api/offers/":
            return board
        raise _http_error(404)

    result = resolve_company(CompanyRef("1", "Acme"), fetch)
    assert result.status is ImportStatus.RESOLVED
    assert (result.board_type, result.slug, result.open_roles) == ("recruitee", "acme", 2)


def test_a_board_with_roles_beats_an_empty_tenant_on_another_vendor() -> None:
    empty_personio = "<workzag-jobs></workzag-jobs>"
    teamtailor = (FIXTURES / "teamtailor_board.xml").read_text()

    def fetch(url: str) -> Any:
        if url == "https://acme.jobs.personio.de/xml":
            return empty_personio
        if url == "https://acme.teamtailor.com/jobs.rss":
            return teamtailor
        raise _http_error(404)

    result = resolve_company(CompanyRef("1", "Acme"), fetch)
    assert (result.board_type, result.slug, result.open_roles) == ("teamtailor", "acme", 2)


def test_an_empty_board_is_still_resolved_when_nothing_better_exists() -> None:
    def fetch(url: str) -> Any:
        if url == "https://acme.jobs.personio.de/xml":
            return "<workzag-jobs></workzag-jobs>"
        raise _http_error(404)

    result = resolve_company(CompanyRef("1", "Acme"), fetch)
    assert result.status is ImportStatus.RESOLVED
    assert (result.board_type, result.open_roles) == ("personio", 0)


def test_careers_url_names_a_page_a_person_can_open() -> None:
    from career_copilot.domain.company_resolution import careers_url

    assert careers_url("greenhouse", "acme") == "https://boards.greenhouse.io/acme"
    assert careers_url("lever", "acme") == "https://jobs.lever.co/acme"
    assert careers_url("personio", "acme") == "https://acme.jobs.personio.de"
    assert careers_url("unknown-vendor", "acme") is None


def test_slug_candidates_transliterate_umlauts_and_handle_ampersands() -> None:
    slugs = slug_candidates("Müller & Söhne GmbH")
    assert "mueller-and-soehne" in slugs
    assert "muller-and-sohne" in slugs
    assert "mueller-soehne" in slugs


def test_slug_candidates_drop_a_domain_suffix() -> None:
    assert "acme" in slug_candidates("Acme.com")
    assert "acme" in slug_candidates("acme.io")
