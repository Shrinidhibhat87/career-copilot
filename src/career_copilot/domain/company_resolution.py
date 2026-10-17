"""Resolve a company-list name to a verified ATS board. No LLM, no search — the same
free, deterministic verification `probe_ats` performs against the real board APIs.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from urllib.parse import urlparse

import httpx

from career_copilot.adapters.sources.ats import SOURCE_TYPES, BoardNotFound, Fetcher
from career_copilot.domain.sources import CompanyRef, ImportStatus, normalize_company_name

# Greenhouse hosts boards at several domains (classic, `job-boards`, and the EU region);
# the public `boards-api.greenhouse.io` serves all of them, so only the slug matters.
_URL_HINTS: dict[str, tuple[re.Pattern[str], ...]] = {
    "greenhouse": (
        re.compile(r"greenhouse\.io/embed/job_board\?(?:[^#]*&)?for=([^&#/]+)", re.IGNORECASE),
        re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?!embed)([^/?#]+)", re.IGNORECASE),
    ),
    "lever": (re.compile(r"jobs\.lever\.co/([^/?#]+)", re.IGNORECASE),),
    "ashby": (re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.IGNORECASE),),
    "personio": (
        re.compile(r"(?<![\w.-])(?!www\.)([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.IGNORECASE),
    ),
    "workable": (
        re.compile(r"apply\.workable\.com/(?!api/|j/)([^/?#]+)", re.IGNORECASE),
        re.compile(r"(?<![\w.-])(?!www\.|apply\.)([a-z0-9-]+)\.workable\.com", re.IGNORECASE),
    ),
    "teamtailor": (
        re.compile(r"(?<![\w.-])(?!www\.)([a-z0-9-]+)\.teamtailor\.com", re.IGNORECASE),
    ),
    "smartrecruiters": (
        re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", re.IGNORECASE),
    ),
    "recruitee": (re.compile(r"(?<![\w.-])(?!www\.)([a-z0-9-]+)\.recruitee\.com", re.IGNORECASE),),
}

# The page a person opens for each vendor's board, as opposed to `url_for`, which is the
# JSON API the crawler reads. Keep in step with `_URL_HINTS`: each template must round-trip
# through `extract_ats_hint`.
_CAREERS_PAGES: dict[str, str] = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "personio": "https://{slug}.jobs.personio.de",
    "workable": "https://apply.workable.com/{slug}",
    "teamtailor": "https://{slug}.teamtailor.com",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
    "recruitee": "https://{slug}.recruitee.com",
}


def careers_url(board_type: str, slug: str) -> str | None:
    """The human-readable careers page of a board, or `None` for a vendor we do not map."""
    template = _CAREERS_PAGES.get(board_type)
    return template.format(slug=slug) if template else None


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_URL_LIKE = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)

# Trailing words that are a legal form or a descriptor, not part of the board's slug
# ("Peec AI" -> `peec`, "ARX Robotics" -> `arx`). Stripped one at a time, so the fullest
# name is always probed before any shorter one.
_TRAILING_NOISE = frozenset(
    {
        "ai", "io", "gmbh", "ag", "se", "inc", "llc", "ltd", "corp", "corporation",
        "robotics", "technologies", "technology", "tech", "labs", "software", "solutions",
        "group", "holding", "gmbh&co", "kg",
    }
)  # fmt: skip
_LEGAL_FORMS = frozenset({"gmbh", "ag", "se", "inc", "llc", "ltd", "corp", "kg"})

# A careers URL on one of these says nothing about the company that listed it.
_NOT_A_COMPANY_HOST = frozenset(
    {
        "linkedin", "notion", "github", "google", "docs", "drive", "twitter", "x",
        "facebook", "instagram", "youtube", "medium", "wikipedia", "crunchbase",
        "greenhouse", "lever", "ashbyhq", "workable", "personio", "smartrecruiters",
        "bamboohr", "join", "recruitee", "teamtailor", "indeed", "glassdoor", "xing",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ResolvedCompany:
    ref: CompanyRef
    status: ImportStatus
    board_type: str | None = None
    slug: str | None = None
    last_error: str | None = None
    # Jobs on the board when it was found. The probe already downloads the whole board,
    # so counting costs nothing; `None` when no board was found.
    open_roles: int | None = None


@dataclass(frozen=True, slots=True)
class Probe:
    exists: bool
    blocked_reason: str | None = None
    open_roles: int | None = None


def extract_ats_hint(url: str | None) -> tuple[str, str] | None:
    """A URL already naming a known ATS board is a free shortcut past slug-guessing."""
    if not url:
        return None
    for board_type, patterns in _URL_HINTS.items():
        for pattern in patterns:
            match = pattern.search(url)
            if match:
                return board_type, match.group(1)
    return None


def domain_label(url: str | None) -> str | None:
    """The registrable-ish label of a company's own website (`www.deliverect.com` ->
    `deliverect`). `None` for an ATS/social host, which names nobody in particular."""
    if not url:
        return None
    parsed = urlparse(url if "//" in url else f"//{url}")
    labels = [part for part in (parsed.hostname or "").lower().split(".") if part]
    if labels and labels[0] in {"www", "careers", "jobs", "career"}:
        labels = labels[1:]
    if len(labels) < 2:
        return None
    label = labels[-3] if len(labels) >= 3 and labels[-2] in {"co", "com", "org"} else labels[-2]
    if not label or label in _NOT_A_COMPANY_HOST:
        return None
    return _NON_ALNUM.sub("", label) or None


_GERMAN_TRANSLITERATION = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_DOMAIN_SUFFIX = re.compile(r"\.(com|io|ai|co|de|net|org|app|dev)$")


def _name_variants(base: str) -> tuple[str, ...]:
    """The name as written, then spellings a board slug might use for it: a trailing
    domain suffix dropped ("acme.com"), umlauts as German writes them (ü -> ue) and as
    plain letters (ü -> u), and "&" as "and" or left out. A slug is ASCII, a name often
    is not, and "Müller & Söhne" is `mueller-and-soehne` on one board and `muller-sohne`
    on another."""
    stem = _DOMAIN_SUFFIX.sub("", base)
    folded = "".join(
        ch for ch in unicodedata.normalize("NFKD", stem) if not unicodedata.combining(ch)
    )
    variants = [
        base,
        stem,
        stem.translate(_GERMAN_TRANSLITERATION),
        folded,
        stem.replace("&", " and ").translate(_GERMAN_TRANSLITERATION),
        folded.replace("&", " and "),
    ]
    return tuple(dict.fromkeys(variant for variant in variants if variant.strip()))


def slug_candidates(name: str, url: str | None = None) -> tuple[str, ...]:
    """Most specific first: the full name, then the name with trailing legal/descriptor
    words peeled off one at a time, then legal-form variants, then the company's own
    domain label. Each is tried hyphenated and squashed, for each spelling of the name.
    """
    candidates: list[str] = []
    for variant in _name_variants(normalize_company_name(name).lower()):
        for slug in _slugs_for(variant):
            if slug not in candidates:
                candidates.append(slug)
    label = domain_label(url)
    if label and label not in candidates:
        candidates.append(label)
    return tuple(candidates)


def _slugs_for(base: str) -> list[str]:
    words = [word for word in _NON_ALNUM.split(base) if word]
    candidates: list[str] = []

    def add(parts: list[str]) -> None:
        for joined in ("-".join(parts), "".join(parts)):
            if joined and joined not in candidates:
                candidates.append(joined)

    add(words)
    stripped = list(words)
    while len(stripped) > 1 and stripped[-1] in _TRAILING_NOISE:
        stripped.pop()
        add(stripped)
    if not stripped:
        return candidates
    # "ARX Robotics" lists its Greenhouse board as `arxroboticsgmbh`, "ZeroG" its
    # Personio board as `zerog-gmbh`.
    if words and words[-1] not in _LEGAL_FORMS:
        for joined in ("".join(words) + "gmbh", "-".join(words) + "-gmbh"):
            if joined not in candidates:
                candidates.append(joined)
    return candidates


def probe_board(fetch: Fetcher, board_type: str, slug: str) -> Probe:
    """`blocked_reason` is None for a clean 404 miss — anything
    else (a non-404 status, a timeout, a connection error, an unparseable body) is a
    signal that a board may exist but couldn't be verified, not that one doesn't exist.
    """
    source = SOURCE_TYPES[board_type]()
    try:
        payload = fetch(source.url_for(slug))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return Probe(False)
        return Probe(False, f"HTTP {exc.response.status_code}")
    except httpx.TimeoutException:
        return Probe(False, "timed out")
    except httpx.TransportError as exc:
        return Probe(False, f"connection error ({type(exc).__name__})")
    except Exception as exc:
        return Probe(False, f"unexpected error ({type(exc).__name__})")
    try:
        roles = len(list(source.parse(payload, slug)))
    except BoardNotFound:
        return Probe(False)
    except Exception as exc:
        return Probe(False, f"unexpected response shape ({type(exc).__name__})")
    return Probe(True, None, roles)


def _name_that_is_a_url(ref: CompanyRef) -> CompanyRef:
    """Some lists put a careers link where the name belongs. Treat it as the URL, and name
    the company after its domain, so the rest of resolution sees an ordinary row."""
    if ref.url or not _URL_LIKE.match(ref.name):
        return ref
    label = domain_label(ref.name)
    return replace(ref, name=label or ref.name, url=ref.name)


def resolve_company(ref: CompanyRef, fetch: Fetcher) -> ResolvedCompany:
    """Try the URL hint first, then slug variants against every known board type. A
    candidate that fails cleanly short-circuits nothing: resolution keeps trying every
    one until a board with open roles turns up, settles for an empty board if that is all
    there is, and only reports `blocked` if nothing ever resolves. An unresolved outcome says
    which slugs were tried, so "no public board found" is checkable by hand.
    """
    probed = ref
    ref = _name_that_is_a_url(ref)
    blocked_reason: str | None = None
    tried: list[str] = []

    hint = extract_ats_hint(ref.url)
    if hint is not None:
        board_type, slug = hint
        tried.append(f"{board_type}/{slug}")
        probe = probe_board(fetch, board_type, slug)
        if probe.exists:
            return ResolvedCompany(
                probed, ImportStatus.RESOLVED, board_type, slug, open_roles=probe.open_roles
            )
        blocked_reason = (
            f"{board_type}/{slug}: {probe.blocked_reason}" if probe.blocked_reason else None
        )

    # A board with no open roles is only a fallback: companies often keep an empty tenant
    # on one vendor while hiring on another, so keep looking for one that has roles.
    empty_board: ResolvedCompany | None = None
    for slug in slug_candidates(ref.name, ref.url):
        tried.append(slug)
        for board_type in SOURCE_TYPES:
            probe = probe_board(fetch, board_type, slug)
            if probe.exists:
                found = ResolvedCompany(
                    probed, ImportStatus.RESOLVED, board_type, slug, open_roles=probe.open_roles
                )
                if probe.open_roles:
                    return found
                empty_board = empty_board or found
            elif probe.blocked_reason and blocked_reason is None:
                blocked_reason = f"{board_type}/{slug}: {probe.blocked_reason}"

    if empty_board:
        return empty_board
    if blocked_reason:
        return ResolvedCompany(probed, ImportStatus.BLOCKED, last_error=blocked_reason)
    return ResolvedCompany(
        probed,
        ImportStatus.UNRESOLVED,
        last_error=f"no public board found (tried: {', '.join(tried)})",
    )
