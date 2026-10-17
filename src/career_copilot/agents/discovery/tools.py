"""The five things the discovery agent can do, and what each of them refuses.

Every cap here answers the model with text rather than raising. An agent told "the
search budget is spent, propose what you already verified" finishes with the work it
has done; an agent that raises loses it. The only exceptions are failures the run
cannot continue past, and those belong to the caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from career_copilot.adapters.sources.ats import SOURCE_TYPES, Fetcher, http_fetcher
from career_copilot.domain.budget import BudgetExceeded, CostMeter
from career_copilot.domain.owner_identity import OwnerIdentity
from career_copilot.domain.sources import SourceProposal
from career_copilot.ports import LLMCallError, SearchProvider

BOARD_TYPES = tuple(SOURCE_TYPES)

# One search's worth of spend, configured rather than reported: a reservation has to be
# knowable before the call, exactly as `triage_reserve_eur` is.
SEARCH_RESERVE_EUR = 0.01


@dataclass(slots=True)
class Probe:
    """What a board answered. Kept for the run so `sample_jobs` costs no second fetch."""

    board_type: str
    slug: str
    exists: bool
    job_count: int = 0
    titles: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()


@dataclass(slots=True)
class DiscoveryDeps:
    """Everything the tools need. Passed by pydantic-ai; never global state."""

    search: SearchProvider
    known: set[tuple[str, str]]
    companies: frozenset[str]
    identity: OwnerIdentity
    meter: CostMeter
    now: datetime
    max_eur: float = 0.0
    max_searches: int = 6
    target_proposals: int = 3
    fetcher: Fetcher | None = None
    probes: dict[tuple[str, str], Probe] = field(default_factory=dict)
    proposals: list[SourceProposal] = field(default_factory=list)
    searches: int = 0
    stopped_because: str = ""

    def board_url(self, board_type: str, slug: str) -> str:
        source = SOURCE_TYPES[board_type](self.fetcher or http_fetcher())
        return source.url_for(slug)

    @property
    def done(self) -> bool:
        return len(self.proposals) >= self.target_proposals


def _identity_terms(identity: OwnerIdentity) -> tuple[str, ...]:
    values = (*identity.names, *identity.emails, *identity.phones)
    return tuple(value.lower() for value in values if len(value) > 2)


def web_search(deps: DiscoveryDeps, query: str, max_results: int = 5) -> str:
    """The only paid, outward call. Refuses to search for the person who owns the resume.

    The guard is deterministic rather than an instruction in the prompt: the agent has
    no reason to search for the user, and a boundary that depends on prompt discipline
    is not a boundary.
    """
    lowered = query.lower()
    for term in _identity_terms(deps.identity):
        if term in lowered:
            return "refused: a query may not contain the resume owner's own details"
    if deps.searches >= deps.max_searches:
        return (
            f"refused: the search cap of {deps.max_searches} is spent — "
            "propose what you already verified"
        )
    try:
        deps.meter.reserve(deps.max_eur, SEARCH_RESERVE_EUR)
    except BudgetExceeded as exc:
        deps.stopped_because = str(exc)
        return f"refused: {exc}"
    deps.searches += 1
    try:
        results = deps.search.search(query, max_results)
    except LLMCallError as exc:
        return f"search failed: {exc}"
    deps.meter.add(SEARCH_RESERVE_EUR)
    if not results:
        return "no results"
    return "\n".join(
        f"- {result.title} | {result.url} | {result.snippet[:200]}" for result in results
    )


def probe_ats(deps: DiscoveryDeps, board_type: str, slug: str) -> str:
    """Verify a guess against the real board API. Free, and the only thing that decides.

    A 404, a non-JSON body or a timeout is `exists: false`. None of them is an
    exception, because a wrong guess is the expected case and must not end the run.
    """
    if board_type not in SOURCE_TYPES:
        return f"unknown board type '{board_type}' — one of {', '.join(BOARD_TYPES)}"
    slug = slug.strip().strip("/")
    if not slug:
        return "empty slug"
    fetch = deps.fetcher or http_fetcher()
    try:
        payload = fetch(deps.board_url(board_type, slug))
        source = SOURCE_TYPES[board_type](fetch)
        jobs = list(source.parse(payload, slug))
    # Every failure — 404, timeout, HTML instead of JSON — means the same thing: no board.
    except Exception as exc:
        deps.probes[(board_type, slug)] = Probe(board_type, slug, exists=False)
        return f"{board_type}/{slug}: no board ({type(exc).__name__})"
    probe = Probe(
        board_type=board_type,
        slug=slug,
        exists=True,
        job_count=len(jobs),
        titles=tuple(job.title for job in jobs[:20]),
        locations=tuple(job.location for job in jobs[:20]),
    )
    deps.probes[(board_type, slug)] = probe
    return f"{board_type}/{slug}: board exists with {probe.job_count} open jobs"


def sample_jobs(deps: DiscoveryDeps, board_type: str, slug: str, n: int = 5) -> str:
    """Titles and locations from a board already probed, so relevance can be judged.

    A company with two hundred openings and none in the user's cities is a finding, not
    a target, and this is what lets the model see that before spending a proposal.
    """
    probe = deps.probes.get((board_type, slug.strip().strip("/")))
    if probe is None or not probe.exists:
        return "probe_ats this board first"
    if not probe.titles:
        return "the board is empty"
    rows = list(zip(probe.titles, probe.locations, strict=False))[: max(1, n)]
    return "\n".join(f"- {title} | {location}" for title, location in rows)


def known_companies(deps: DiscoveryDeps) -> str:
    """What the store already covers, so a rejected board does not come back next week."""
    slugs = sorted(f"{board}/{slug}" for board, slug in deps.known)
    companies = sorted(deps.companies)
    return (
        "boards already known or already decided: "
        + (", ".join(slugs) or "none")
        + "\ncompanies already in the database: "
        + (", ".join(companies[:80]) or "none")
    )


def propose_source(
    deps: DiscoveryDeps,
    board_type: str,
    slug: str,
    company: str,
    evidence: str,
    confidence: float = 0.5,
) -> str:
    """The terminal action: one pending row, for a board this run actually verified."""
    slug = slug.strip().strip("/")
    probe = deps.probes.get((board_type, slug))
    if probe is None or not probe.exists:
        return f"refused: {board_type}/{slug} was never probed successfully in this run"
    if (board_type, slug) in deps.known:
        return f"refused: {board_type}/{slug} is already known or was already decided"
    if any(p.board_type == board_type and p.slug == slug for p in deps.proposals):
        return f"refused: {board_type}/{slug} was already proposed in this run"
    if not evidence.strip():
        return "refused: evidence is required — name the search result and the job count"
    deps.proposals.append(
        SourceProposal(
            board_type=board_type,
            slug=slug,
            company=company.strip() or slug,
            evidence=evidence.strip(),
            confidence=max(0.0, min(1.0, confidence)),
            job_count=probe.job_count,
            sample_titles=probe.titles[:5],
        )
    )
    if deps.done:
        return (
            f"proposed {company} ({board_type}/{slug}). "
            f"{len(deps.proposals)} proposals is the target — stop now and summarise."
        )
    return f"proposed {company} ({board_type}/{slug}); {len(deps.proposals)} so far"


def preferences_summary(
    role_families: Sequence[str], skills: Sequence[str], cities: Sequence[str]
) -> str:
    """Technology and city vocabulary only. No identity ever reaches the agent's prompt."""
    return (
        f"Roles: {', '.join(role_families) or 'any engineering role'}. "
        f"Core skills: {', '.join(list(skills)[:12]) or 'unspecified'}. "
        f"Cities: {', '.join(cities) or 'unspecified'}."
    )


def profile_summary(archetypes: Sequence[str], similar_companies: Sequence[str]) -> str:
    """What the profiler learned that a board search can use: kinds of role, and companies
    whose roles the user holds up as examples. Company names, never the user's identity."""
    lines = []
    if archetypes:
        lines.append(f"Role archetypes: {', '.join(archetypes)}.")
    if similar_companies:
        lines.append(
            f"Companies hiring for this kind of role: {', '.join(similar_companies)}. "
            "Check their boards first, then look for companies like them."
        )
    return "\n".join(lines)
