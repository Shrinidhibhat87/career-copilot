"""Normalization and dedupe identity. Pure functions."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from career_copilot.domain.models import JobPosting, RawJob

_LEGAL_SUFFIXES = (
    "gmbh & co. kg",
    "gmbh",
    "ug",
    "ag",
    "se",
    "e.v.",
    "kgaa",
    "inc.",
    "inc",
    "llc",
    "ltd.",
    "ltd",
    "limited",
    "b.v.",
    "bv",
    "s.a.",
    "sa",
    "oy",
    "ab",
    "aps",
    "corp.",
    "corp",
    "co.",
)

# "(m/w/d)", "(f/m/x)", "m/w/d", "all genders" and friends.
_GENDER_TAG = re.compile(
    r"\(?\s*(?:[mfwdxa])\s*[/|]\s*[mfwdxa](?:\s*[/|]\s*[mfwdxa])?\s*\)?"
    r"|\(\s*all\s+genders?\s*\)"
    r"|\(\s*any\s+gender\s*\)",
    re.IGNORECASE,
)

_TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gclid|fbclid|msclkid|mc_cid|mc_eid|ref|referrer|source|src|trk|trkCampaign"
    r"|li_fat_id|_ga|igshid|campaign|medium)$",
    re.IGNORECASE,
)

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")

_COUNTRY_TAIL = re.compile(
    r",?\s*(germany|deutschland|de|europe|eu|emea|worldwide|global)\s*$", re.IGNORECASE
)

# A board writes several places into one field, and every board picks its own separator.
# A spaced hyphen only: "Frankfurt am Main" and "e-commerce" must survive intact.
_SEGMENT_SPLIT = re.compile(r"[,;|/]|\s+[-\u2013\u2014]\s+")

_CITY_ALIASES = {
    "munchen": "munich",
    "muenchen": "munich",
    "münchen": "munich",
    "koln": "cologne",
    "köln": "cologne",
    "frankfurt am main": "frankfurt",
    "frankfurt a m": "frankfurt",
}

_REMOTE_HINT = re.compile(r"\bremote\b|\bhome ?office\b|\banywhere\b", re.IGNORECASE)
# What is left of a remote segment once nothing names a place: "Remote", "Remote in", "Home Office".
_BARE_REMOTE = re.compile(r"(?:remote|home ?office|anywhere)(?:\s+(?:in|within))?", re.IGNORECASE)


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def normalize_company(name: str, aliases: dict[str, str] | None = None) -> str:
    """Lowercase, drop legal suffixes and punctuation, then resolve known aliases."""
    value = _collapse(name.lower())
    changed = True
    while changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if value.endswith(" " + suffix):
                value = value[: -len(suffix)].strip(" ,-")
                changed = True
    value = _collapse(_PUNCT.sub(" ", value))
    if aliases:
        value = aliases.get(value, value)
    return value


def normalize_title(title: str) -> str:
    """Lowercase and strip gender tags and punctuation so titles compare across sources."""
    value = _GENDER_TAG.sub(" ", title.lower())
    value = _PUNCT.sub(" ", value)
    return _collapse(value)


def location_segments(location: str) -> tuple[str, ...]:
    """Every place a posting names, as comparable city tokens.

    One posting is often several places — "Berlin, Berlin, Germany" is one city written
    three times, "Berlin; London; Munich" is three. Collapsing either into a single token
    produces a string no city list can contain, which is how a Berlin role ends up
    rejected as being outside Germany. Repeats collapse; order is the board's.

    "Remote" alone is the token `remote`, open anywhere. "Remote US" keeps its region as
    `remote us`, which no allowed-city list contains: a role open only in the US is not
    open in Berlin. "Remote, New York, Canada" has a standalone `remote` among its places,
    so it still qualifies.
    """
    segments: list[str] = []
    for raw in _SEGMENT_SPLIT.split(location.lower()):
        value = _collapse(_PUNCT.sub(" ", raw))
        # A country or region names no city. Repeated because "berlin germany europe"
        # carries two of them, and one pass would leave the other attached.
        while value:
            stripped = _collapse(_COUNTRY_TAIL.sub("", value))
            if stripped == value:
                break
            value = stripped
        if not value:
            continue
        if _BARE_REMOTE.fullmatch(value):
            value = "remote"
        elif not _REMOTE_HINT.search(value):
            value = _CITY_ALIASES.get(value, value)
        # else: "remote us" keeps its region. Only a standalone "remote" is open anywhere.
        if value not in segments:
            segments.append(value)
    return tuple(segments)


def normalize_location(location: str) -> str:
    """The posting's primary city token, or 'remote', or '' when it names no place.

    The first segment, so `location_norm`, `fingerprint` and every display stay what they
    were. Matching reads `location_segments`, which keeps the rest.
    """
    segments = location_segments(location)
    if not segments:
        return ""
    # A qualified remote ("remote us") is still "remote" for identity.
    return "remote" if _REMOTE_HINT.search(segments[0]) else segments[0]


def canonical_url(url: str) -> str:
    """Strip tracking noise while keeping params that identify the posting."""
    parts = urlsplit(url.strip())
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not _TRACKING_PARAMS.match(k)
    ]
    kept.sort()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(kept), ""))


def content_hash(description: str) -> str:
    """Hash of the whitespace-insensitive description, used for change detection."""
    return hashlib.sha256(_collapse(description.lower()).encode()).hexdigest()


def fingerprint(company_norm: str, title_norm: str, location_norm: str) -> str:
    """Cross-source identity for the same posting."""
    return hashlib.sha256(f"{company_norm}|{title_norm}|{location_norm}".encode()).hexdigest()


def is_remote(raw: RawJob) -> bool:
    if raw.remote is not None:
        return raw.remote
    return bool(_REMOTE_HINT.search(raw.location))


def to_posting(raw: RawJob, aliases: dict[str, str] | None = None) -> JobPosting:
    """Normalize a raw source record into a posting with its dedupe identity."""
    company_norm = normalize_company(raw.company, aliases)
    title_norm = normalize_title(raw.title)
    location_norm = normalize_location(raw.location)
    return JobPosting(
        source=raw.source,
        source_job_id=raw.source_job_id,
        company=_collapse(raw.company),
        company_norm=company_norm,
        title=_collapse(raw.title),
        title_norm=title_norm,
        location=_collapse(raw.location),
        location_norm=location_norm,
        canonical_url=canonical_url(raw.url),
        description=raw.description,
        fingerprint=fingerprint(company_norm, title_norm, location_norm),
        content_hash=content_hash(raw.description),
        posted_at=raw.posted_at,
        employment_type=raw.employment_type,
        remote=is_remote(raw),
    )
