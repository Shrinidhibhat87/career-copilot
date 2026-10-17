import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_copilot.usecases.sample_roles import SampleRoleError, resolve_job_url

FIXTURES = Path(__file__).parent.parent / "fixtures"
FDE = "594e7ab1-3c77-43b5-be3f-bc30d513c14c"


def board(name: str) -> Any:
    def fetch(url: str) -> Any:
        return json.loads((FIXTURES / name).read_text())

    return fetch


@pytest.mark.parametrize(
    "url",
    [
        f"https://jobs.ashbyhq.com/taktile/{FDE}",
        f"https://jobs.ashbyhq.com/taktile/{FDE}/application?utm_source=linkedin",
        f"jobs.ashbyhq.com/taktile/{FDE}/",
    ],
)
def test_ashby_url_resolves_to_the_one_job(url: str) -> None:
    source, slug, job = resolve_job_url(url, board("ashby_board.json"))
    assert (source, slug) == ("ashby", "taktile")
    assert job.source_job_id == FDE
    assert job.title == "Forward Deployed Engineer"


def test_greenhouse_url_resolves() -> None:
    _, slug, job = resolve_job_url(
        "https://job-boards.greenhouse.io/acme/jobs/4512346", board("greenhouse_board.json")
    )
    assert slug == "acme"
    assert job.source_job_id == "4512346"


def test_unknown_host_is_refused_without_fetching() -> None:
    def never(url: str) -> Any:
        raise AssertionError("must not fetch")

    with pytest.raises(SampleRoleError, match=r"(?i)paste"):
        resolve_job_url("https://www.linkedin.com/jobs/view/123", never)


def test_closed_posting_is_refused() -> None:
    with pytest.raises(SampleRoleError, match="no longer"):
        resolve_job_url(
            "https://jobs.ashbyhq.com/taktile/99999999-0000-0000-0000-000000000000",
            board("ashby_board.json"),
        )


def test_board_url_without_a_job_id_is_refused() -> None:
    with pytest.raises(SampleRoleError, match="one job"):
        resolve_job_url("https://jobs.ashbyhq.com/taktile", board("ashby_board.json"))


def test_fetch_failure_becomes_a_readable_error() -> None:
    def down(url: str) -> Any:
        raise httpx.ConnectError("boom")

    with pytest.raises(SampleRoleError, match="could not reach"):
        resolve_job_url(f"https://jobs.ashbyhq.com/taktile/{FDE}", down)
