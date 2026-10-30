"""The German federal job board (Bundesagentur für Arbeit "Jobsuche"). Free, no account.

Unlike an ATS board, a target here is a search, not a company: its identifier is a query
string such as `was=Python&wo=Berlin&umkreis=25`. The service sits behind a public key the
agency publishes for its own site and apps. It is not a documented API, so a change on
their side shows up as a failed fetch in `source_runs`, never as zero jobs.

The search lists jobs without their text, so each job costs one more (throttled) request.
`MAX_JOBS` bounds that. Parsing is separate from fetching, as for the ATS adapters, so the
mapping is tested against recorded payloads.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode

from career_copilot.adapters.sources.ats import Fetcher, http_fetcher, strip_html
from career_copilot.domain.models import RawJob, SourceTarget

log = logging.getLogger(__name__)

SERVICE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc"
# The search answers on v6 (v4 answers 403 for it); job details are still served on v4.
SEARCH_BASE = f"{SERVICE}/v6"
DETAIL_BASE = f"{SERVICE}/v4"
# The key the agency's own site sends. Public, not a secret.
_HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": "career-copilot (personal job-search tool)",
}
JOB_PAGE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
MAX_JOBS = 30
# Only these filters are passed on; the rest of the identifier is ignored.
_FILTERS = ("was", "wo", "umkreis", "veroeffentlichtseit", "arbeitszeit", "befristung")
DEFAULT_DAYS = 14


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def query_for(identifier: str) -> dict[str, str]:
    """The filters in an identifier like `was=Python&wo=Berlin`, first value of each."""
    parsed = parse_qs(identifier)
    query = {key: parsed[key][0] for key in _FILTERS if parsed.get(key) and parsed[key][0]}
    query.setdefault("veroeffentlichtseit", str(DEFAULT_DAYS))
    return query


class JobsucheSource:
    name = "jobsuche"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher(headers=_HEADERS)

    def search_url(self, identifier: str) -> str:
        query = {**query_for(identifier), "size": str(MAX_JOBS), "page": "1"}
        return f"{SEARCH_BASE}/jobs?{urlencode(query)}"

    def detail_url(self, reference: str) -> str:
        return f"{DETAIL_BASE}/jobdetails/{base64.b64encode(reference.encode()).decode()}"

    def ping(self) -> int:
        """One request for one result: how many postings a plain search matches. Raises
        when the service is unreachable or has changed, which is what `doctor` reports."""
        query = urlencode({"was": "Software", "size": "1", "page": "1"})
        return int(self._fetch(f"{SEARCH_BASE}/jobs?{query}").get("maxErgebnisse") or 0)

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        listed = self._fetch(self.search_url(target.identifier)).get("ergebnisliste") or []
        jobs: list[RawJob] = []
        for entry in listed[:MAX_JOBS]:
            reference = entry.get("referenznummer")
            if not reference:
                continue
            try:
                detail = self._fetch(self.detail_url(reference))
            except Exception as error:  # one bad job must not lose the rest of the search
                log.warning("jobsuche detail for %s failed: %s", reference, error)
                continue
            jobs.append(self.parse(entry, detail))
        return jobs

    def parse(self, entry: dict[str, Any], detail: dict[str, Any]) -> RawJob:
        reference = str(entry["referenznummer"])
        place = ((entry.get("stellenlokationen") or [{}])[0].get("adresse")) or {}
        home_office = entry.get("homeofficemoeglich")
        return RawJob(
            source=self.name,
            source_job_id=reference,
            company=(entry.get("firma") or detail.get("firma") or "").strip(),
            title=entry.get("stellenangebotsTitel", ""),
            location=", ".join(part for part in (place.get("ort"), place.get("land")) if part),
            url=f"{JOB_PAGE}{reference}",
            description=strip_html(detail.get("stellenangebotsBeschreibung", "")),
            posted_at=_parse_dt(entry.get("datumErsteVeroeffentlichung")),
            employment_type=_employment_type(entry),
            remote=True if home_office else None,
        )


def _employment_type(entry: dict[str, Any]) -> str | None:
    if entry.get("arbeitszeitVollzeit"):
        return "full-time"
    if any(entry.get(key) for key in entry if key.startswith("arbeitszeitTeilzeit")):
        return "part-time"
    return None


# Sources whose targets are searches, not company boards. Kept apart from `SOURCE_TYPES`
# because the resolver and the discovery agent probe a company slug against that one.
SEARCH_SOURCES: dict[str, type[JobsucheSource]] = {JobsucheSource.name: JobsucheSource}
