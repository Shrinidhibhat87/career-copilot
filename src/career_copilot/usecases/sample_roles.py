"""Turn a pasted job link into the posting it names.

No vendor publishes a stable single-job API, so the board is fetched the way a crawl does
and the job picked out by its id. A job id is whatever the board calls `source_job_id`,
and every supported vendor puts it in the job's own URL, as a path segment or a query
value. Matching on that is what makes tracking parameters and `/application` suffixes
harmless.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

import httpx

from career_copilot.adapters.sources.ats import SOURCE_TYPES, BoardNotFound, Fetcher
from career_copilot.domain.company_resolution import extract_ats_hint
from career_copilot.domain.models import RawJob, SourceTarget

# Path words vendors put around a job id. A link made only of these and the slug names
# the whole board, not one job, so it is refused before anything is fetched.
_NOT_A_JOB_ID = frozenset({"jobs", "job", "j", "o", "application", "apply"})


class SampleRoleError(ValueError):
    """The link could not be used. The message is written for the person who pasted it."""


def _url_tokens(url: str) -> set[str]:
    """Every path segment and query value: one of them is the job id, wherever the
    vendor puts it."""
    parsed = urlparse(url if "//" in url else f"//{url}")
    tokens = {part for part in parsed.path.split("/") if part}
    tokens |= {value for _, value in parse_qsl(parsed.query) if value}
    return tokens


def resolve_job_url(url: str, fetch: Fetcher) -> tuple[str, str, RawJob]:
    """`(board, slug, job)` for a link to one posting on a supported board.

    Raises `SampleRoleError` for anything else, including a board that cannot be
    reached: the caller then asks for pasted text instead of failing.
    """
    hint = extract_ats_hint(url)
    if hint is None or hint[0] not in SOURCE_TYPES:
        raise SampleRoleError(
            "That is not a job board I can read. Paste the job's title and text instead."
        )
    board, slug = hint
    tokens = _url_tokens(url) - {slug}
    if not tokens - _NOT_A_JOB_ID:
        raise SampleRoleError("That link is a whole board. Paste the link to one job.")
    try:
        jobs = list(SOURCE_TYPES[board](fetch).fetch(SourceTarget(board, slug)))
    # ValueError covers a body that is not the JSON the adapter expects: to the person
    # pasting the link, that is the same "could not read the board" as a timeout.
    except (httpx.HTTPError, BoardNotFound, ValueError) as error:
        raise SampleRoleError(
            f"I could not reach the {board} board for {slug} ({type(error).__name__}). "
            "Paste the job's title and text instead."
        ) from error
    for job in jobs:
        if job.source_job_id in tokens:
            return board, slug, job
    raise SampleRoleError("That job is no longer on the board. Paste its title and text instead.")
