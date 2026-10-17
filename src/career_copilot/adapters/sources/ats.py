"""Tier-0 sources: public ATS board APIs. Free, structured, ToS-clean.

Each adapter maps one board's JSON to RawJob. Parsing is separated from
fetching so the mapping can be tested against recorded fixtures offline.
"""

from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from career_copilot.domain.models import RawJob, SourceTarget

_TAG = re.compile(r"<[^>]+>")


def strip_html(value: str) -> str:
    """Greenhouse returns entity-escaped HTML, so unescape before stripping tags."""
    text = unescape(value or "")
    text = _TAG.sub(" ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class BoardNotFound(Exception):
    """The vendor answered, but there is no board for this slug. Some vendors say so with
    a 200 instead of a 404 (SmartRecruiters), so the adapter raises this and `probe_board`
    reads it as the same clean miss a 404 is."""


def _local(tag: str) -> str:
    """An XML tag without its `{namespace}` prefix."""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _joined(*parts: str) -> str:
    return ", ".join(part for part in parts if part)


class Fetcher(Protocol):
    """Indirection so tests inject recorded payloads instead of hitting the network."""

    def __call__(self, url: str) -> Any: ...


# Hosts that answer a burst of probes with 429 get a minimum gap between requests.
_MIN_INTERVAL_SECONDS = {"apply.workable.com": 1.0}
_MAX_429_RETRIES = 2
_MAX_RETRY_WAIT_SECONDS = 10.0


class HostThrottle:
    """Spaces requests to one host, across threads. Slots are reserved under the lock and
    slept off outside it, so a queue of workers drains at the allowed rate."""

    def __init__(
        self,
        intervals: dict[str, float],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._intervals = intervals
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_free: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}

    def wait(self, host: str) -> None:
        interval = self._intervals.get(host)
        if not interval:
            return
        with self._lock:
            now = self._clock()
            slot = max(self._next_free.get(host, now), now)
            self._next_free[host] = slot + interval
        if slot > now:
            self._sleep(slot - now)

    def block(self, host: str, seconds: float) -> None:
        """A host told us to stay away for `seconds`: stop asking until then."""
        with self._lock:
            self._blocked_until[host] = self._clock() + seconds

    def blocked_for(self, host: str) -> float:
        """Seconds left on a block, `0.0` when the host is not blocked."""
        with self._lock:
            return max(0.0, self._blocked_until.get(host, 0.0) - self._clock())


_THROTTLE = HostThrottle(_MIN_INTERVAL_SECONDS)


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429, honouring `Retry-After`. A host that asks
    for more than `_MAX_RETRY_WAIT_SECONDS` has blocked us for a long while (Workable
    answers 86400 after a burst), so the caller gives up and reports the 429."""
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return 2.0 * (attempt + 1)


def _blocked(url: str) -> httpx.HTTPStatusError:
    """The 429 a blocked host would have answered, raised without sending a request."""
    request = httpx.Request("GET", url)
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("host is rate-limiting us", request=request, response=response)


def http_fetcher(timeout: float = 20.0, throttle: HostThrottle | None = None) -> Fetcher:
    throttle = throttle or _THROTTLE

    def fetch(url: str) -> Any:
        host = urlparse(url).hostname or ""
        # A host that banned us for a long while is not asked again for the rest of the
        # run: every further request is slow, pointless, and risks extending the ban.
        if throttle.blocked_for(host) > 0:
            raise _blocked(url)
        for attempt in range(_MAX_429_RETRIES + 1):
            throttle.wait(host)
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
            if response.status_code != 429 or attempt == _MAX_429_RETRIES:
                break
            wait = _retry_wait(response, attempt)
            if wait > _MAX_RETRY_WAIT_SECONDS:
                throttle.block(host, wait)
                break
            time.sleep(wait)
        response.raise_for_status()
        # Personio and Teamtailor publish XML; the rest publish JSON.
        if "xml" in response.headers.get("content-type", "").lower():
            return response.text
        return response.json()

    return fetch


class GreenhouseSource:
    """Greenhouse job board API: boards-api.greenhouse.io/v1/boards/<slug>/jobs."""

    name = "greenhouse"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher()

    def url_for(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        payload = self._fetch(self.url_for(target.identifier))
        return list(self.parse(payload, target.identifier))

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]:
        for job in payload.get("jobs", []):
            offices = job.get("offices") or []
            location = (job.get("location") or {}).get("name") or (
                offices[0].get("name") if offices else ""
            )
            company = (job.get("company_name") or slug).strip()
            yield RawJob(
                source=self.name,
                source_job_id=str(job["id"]),
                company=company,
                title=job.get("title", ""),
                location=location or "",
                url=job.get("absolute_url", ""),
                description=strip_html(job.get("content", "")),
                posted_at=_parse_dt(job.get("updated_at") or job.get("first_published")),
            )


class LeverSource:
    """Lever postings API: api.lever.co/v0/postings/<slug>?mode=json."""

    name = "lever"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher()

    def url_for(self, slug: str) -> str:
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        payload = self._fetch(self.url_for(target.identifier))
        return list(self.parse(payload, target.identifier))

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]:
        for job in payload:
            categories = job.get("categories") or {}
            posted = job.get("createdAt")
            yield RawJob(
                source=self.name,
                source_job_id=str(job["id"]),
                company=slug,
                title=job.get("text", ""),
                location=categories.get("location") or "",
                url=job.get("hostedUrl", ""),
                description=strip_html(job.get("descriptionPlain") or job.get("description") or ""),
                posted_at=datetime.fromtimestamp(posted / 1000) if posted else None,
                employment_type=categories.get("commitment"),
                remote=(categories.get("workplaceType") or "").lower() == "remote" or None,
            )


class AshbySource:
    """Ashby job board API: api.ashbyhq.com/posting-api/job-board/<slug>."""

    name = "ashby"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher()

    def url_for(self, slug: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        payload = self._fetch(self.url_for(target.identifier))
        return list(self.parse(payload, target.identifier))

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]:
        for job in payload.get("jobs", []):
            yield RawJob(
                source=self.name,
                source_job_id=str(job["id"]),
                company=slug,
                title=job.get("title", ""),
                location=job.get("location") or "",
                url=job.get("jobUrl") or job.get("applyUrl") or "",
                description=strip_html(
                    job.get("descriptionPlain") or job.get("descriptionHtml") or ""
                ),
                posted_at=_parse_dt(job.get("publishedAt")),
                employment_type=job.get("employmentType"),
                remote=job.get("isRemote"),
            )


class WorkableSource:
    """Workable widget API: apply.workable.com/api/v1/widget/accounts/<slug>."""

    name = "workable"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher()

    def url_for(self, slug: str) -> str:
        return f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        payload = self._fetch(self.url_for(target.identifier))
        return list(self.parse(payload, target.identifier))

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]:
        jobs = payload.get("jobs") or []
        # An unknown account may answer 200 with no name and no jobs.
        if not jobs and not payload.get("name"):
            raise BoardNotFound(slug)
        company = (payload.get("name") or slug).strip()
        for job in jobs:
            yield RawJob(
                source=self.name,
                source_job_id=str(job.get("shortcode") or job["id"]),
                company=company,
                title=job.get("title", ""),
                location=_joined(job.get("city") or "", job.get("country") or ""),
                url=job.get("url") or job.get("shortlink") or "",
                description=strip_html(job.get("description") or ""),
                posted_at=_parse_dt(job.get("published_on") or job.get("created_at")),
                employment_type=job.get("employment_type"),
                remote=job.get("telecommuting") or None,
            )


class RecruiteeSource:
    """Recruitee careers API: <slug>.recruitee.com/api/offers/."""

    name = "recruitee"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher()

    def url_for(self, slug: str) -> str:
        return f"https://{slug}.recruitee.com/api/offers/"

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        payload = self._fetch(self.url_for(target.identifier))
        return list(self.parse(payload, target.identifier))

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]:
        if "offers" not in payload:
            raise BoardNotFound(slug)
        for offer in payload["offers"]:
            if offer.get("status") not in (None, "published"):
                continue
            description = " ".join(
                [offer.get("description") or "", offer.get("requirements") or ""]
            )
            posted = (offer.get("published_at") or "").replace(" UTC", "+00:00")
            yield RawJob(
                source=self.name,
                source_job_id=str(offer["id"]),
                company=offer.get("company_name") or slug,
                title=offer.get("title", ""),
                location=offer.get("location")
                or _joined(offer.get("city") or "", offer.get("country") or ""),
                url=offer.get("careers_url") or "",
                description=strip_html(description),
                posted_at=_parse_dt(posted),
                employment_type=offer.get("employment_type_code"),
                remote=offer.get("remote") or None,
            )


class SmartRecruitersSource:
    """SmartRecruiters posting API: api.smartrecruiters.com/v1/companies/<slug>/postings.

    The list carries no description, so `fetch` reads each posting's detail too, up to
    `MAX_DETAILS`. An unknown company answers 200 with `totalFound: 0`, which `parse`
    reports as `BoardNotFound`; `fetch` treats it as an empty board, since a real company
    can have no open roles.
    """

    name = "smartrecruiters"
    PAGE_SIZE = 100
    MAX_DETAILS = 100

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher()

    def url_for(self, slug: str) -> str:
        return (
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={self.PAGE_SIZE}"
        )

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        slug = target.identifier
        payload = self._fetch(self.url_for(slug))
        postings = list(payload.get("content") or [])
        total = payload.get("totalFound", 0)
        while postings and len(postings) < min(total, self.MAX_DETAILS):
            page = self._fetch(f"{self.url_for(slug)}&offset={len(postings)}").get("content")
            if not page:
                break
            postings.extend(page)
        for posting in postings[: self.MAX_DETAILS]:
            try:
                url = (
                    f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting['id']}"
                )
                posting["_detail"] = self._fetch(url)
            except httpx.HTTPError:
                continue  # keep the posting; it just scores without a description
        payload["content"] = postings
        try:
            return list(self.parse(payload, slug))
        except BoardNotFound:
            return []

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]:
        if not payload.get("totalFound"):
            raise BoardNotFound(slug)
        for posting in payload.get("content") or []:
            detail = posting.get("_detail") or {}
            sections = ((detail.get("jobAd") or {}).get("sections")) or {}
            description = " ".join((section or {}).get("text", "") for section in sections.values())
            location = posting.get("location") or {}
            yield RawJob(
                source=self.name,
                source_job_id=str(posting["id"]),
                company=(posting.get("company") or {}).get("name") or slug,
                title=posting.get("name", ""),
                location=location.get("fullLocation")
                or _joined(location.get("city") or "", (location.get("country") or "").upper()),
                url=detail.get("postingUrl")
                or f"https://jobs.smartrecruiters.com/{slug}/{posting['id']}",
                description=strip_html(description),
                posted_at=_parse_dt(posting.get("releasedDate")),
                employment_type=(posting.get("typeOfEmployment") or {}).get("label"),
                remote=location.get("remote") or None,
            )


class PersonioSource:
    """Personio XML feed: <slug>.jobs.personio.de/xml (redirects to .com for some tenants)."""

    name = "personio"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher()

    def url_for(self, slug: str) -> str:
        return f"https://{slug}.jobs.personio.de/xml"

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        payload = self._fetch(self.url_for(target.identifier))
        return list(self.parse(payload, target.identifier))

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]:
        try:
            root = ET.fromstring(payload)
        except (ET.ParseError, TypeError, ValueError) as exc:
            raise BoardNotFound(slug) from exc
        if _local(root.tag) != "workzag-jobs":
            raise BoardNotFound(slug)
        for position in root:
            if _local(position.tag) != "position":
                continue
            job_id = _child_text(position, "id")
            sections = [
                _child_text(section, "value")
                for descriptions in position
                if _local(descriptions.tag) == "jobDescriptions"
                for section in descriptions
            ]
            yield RawJob(
                source=self.name,
                source_job_id=job_id,
                company=_child_text(position, "subcompany") or slug,
                title=_child_text(position, "name"),
                location=_child_text(position, "office"),
                url=f"https://{slug}.jobs.personio.de/job/{job_id}",
                description=strip_html(" ".join(sections)),
                posted_at=_parse_dt(_child_text(position, "createdAt")),
                employment_type=_child_text(position, "schedule") or None,
            )


class TeamtailorSource:
    """Teamtailor RSS feed: <slug>.teamtailor.com/jobs.rss."""

    name = "teamtailor"

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self._fetch = fetcher or http_fetcher()

    def url_for(self, slug: str) -> str:
        return f"https://{slug}.teamtailor.com/jobs.rss"

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        payload = self._fetch(self.url_for(target.identifier))
        return list(self.parse(payload, target.identifier))

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]:
        try:
            root = ET.fromstring(payload)
        except (ET.ParseError, TypeError, ValueError) as exc:
            raise BoardNotFound(slug) from exc
        if _local(root.tag) != "rss":
            raise BoardNotFound(slug)
        channel = next((child for child in root if _local(child.tag) == "channel"), root)
        company = _child_text(channel, "title") or slug
        for item in channel:
            if _local(item.tag) != "item":
                continue
            cities = [
                _child_text(location, "city") or _child_text(location, "name")
                for locations in item
                if _local(locations.tag) == "locations"
                for location in locations
            ]
            published = _child_text(item, "pubDate")
            try:
                posted_at = parsedate_to_datetime(published) if published else None
            except (TypeError, ValueError):
                posted_at = None
            remote = _child_text(item, "remoteStatus").lower()
            yield RawJob(
                source=self.name,
                source_job_id=_child_text(item, "guid") or _child_text(item, "link"),
                company=company,
                title=_child_text(item, "title"),
                location=", ".join(dict.fromkeys(city for city in cities if city)),
                url=_child_text(item, "link"),
                description=strip_html(_child_text(item, "description")),
                posted_at=posted_at,
                remote=True if remote in {"fully", "full", "remote"} else None,
            )


class BoardAdapter(Protocol):
    """What the resolver and discovery tools need from any vendor's adapter."""

    name: str

    def __init__(self, fetcher: Fetcher | None = None) -> None: ...

    def url_for(self, slug: str) -> str: ...

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]: ...

    def parse(self, payload: Any, slug: str) -> Iterable[RawJob]: ...


# Dict order is the order the resolver probes a slug, so most common vendors come first.
SOURCE_TYPES: dict[str, type[BoardAdapter]] = {
    GreenhouseSource.name: GreenhouseSource,
    LeverSource.name: LeverSource,
    AshbySource.name: AshbySource,
    PersonioSource.name: PersonioSource,
    TeamtailorSource.name: TeamtailorSource,
    SmartRecruitersSource.name: SmartRecruitersSource,
    RecruiteeSource.name: RecruiteeSource,
    # Last: Workable rate-limits probes, so only ask it once the others have said no.
    WorkableSource.name: WorkableSource,
}
