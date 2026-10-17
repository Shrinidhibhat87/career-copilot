"""The board-discovery run: an agent proposes, the store records, the user decides.

Deliberately not part of `pipeline.py`. A discovery run's determinism is what makes its
output trustworthy, and board discovery is a weekly-or-rarer activity whose cost should
not be paid by every daily crawl.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from pydantic_ai import Agent, AgentRunResult, UsageLimitExceeded
from pydantic_ai.exceptions import AgentRunError, ModelAPIError
from pydantic_ai.models import Model

from career_copilot.adapters.sources.ats import Fetcher
from career_copilot.agents.discovery.agent import (
    DiscoveryLimits,
    build_agent,
    usage_limits,
)
from career_copilot.agents.discovery.tools import (
    DiscoveryDeps,
    preferences_summary,
    profile_summary,
)
from career_copilot.agents.recording import AgentRecording
from career_copilot.domain.budget import BudgetExceeded, CostMeter, RunLimits
from career_copilot.domain.owner_identity import OwnerIdentity
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.search_profile import SearchProfile
from career_copilot.domain.sources import SourceProposal
from career_copilot.ports import LLMCallError, SearchProvider, SourceCatalog

log = logging.getLogger(__name__)


class SearchUnavailable(RuntimeError):
    """No search provider. Slug-guessing alone can only re-derive companies already known."""


@dataclass(frozen=True, slots=True)
class DiscoverSourcesReport:
    proposals: tuple[SourceProposal, ...]
    stored: int
    duplicates: int
    searches: int
    cost_eur: float
    stopped_because: str
    summary: str

    @property
    def found(self) -> int:
        return len(self.proposals)


@dataclass(slots=True)
class DiscoverSourcesRun:
    agent: Agent[DiscoveryDeps, str]
    store: SourceCatalog
    search: SearchProvider
    preferences: Preferences
    identity: OwnerIdentity
    now: datetime
    run_id: int | None = None
    target_proposals: int = 3
    max_searches: int = 6
    max_eur: float = 0.0
    limits: DiscoveryLimits = field(default_factory=DiscoveryLimits)
    run_limits: RunLimits = field(default_factory=lambda: RunLimits(max_seconds=600))
    fetcher: Fetcher | None = None
    persist: bool = True
    # The profiler's reading of what the user wants; `None` before one has been built.
    profile: SearchProfile | None = None
    recording: AgentRecording | None = None
    # One company by name: the run looks for that company's board and nothing else.
    company: str | None = None

    def execute(self) -> DiscoverSourcesReport:
        deps = self._deps()
        summary, stopped = self._invoke(deps)
        stored, duplicates = self._persist(deps.proposals) if self.persist else (0, 0)
        return DiscoverSourcesReport(
            proposals=tuple(deps.proposals),
            stored=stored,
            duplicates=duplicates,
            searches=deps.searches,
            cost_eur=deps.meter.spent,
            stopped_because=stopped or deps.stopped_because,
            summary=summary,
        )

    def _deps(self) -> DiscoveryDeps:
        store = self.store
        companies = frozenset(store.company_aliases().values())
        return DiscoveryDeps(
            search=self.search,
            known=set(store.known_slugs()),
            companies=companies,
            identity=self.identity,
            meter=CostMeter(self.run_limits),
            now=self.now,
            max_eur=self.max_eur,
            max_searches=self.max_searches,
            target_proposals=self.target_proposals,
            fetcher=self.fetcher,
        )

    def _prompt(self) -> str:
        if self.company:
            return (
                f"Find the job board of the company named {self.company!r}. Search the web "
                "for its careers page, verify the board with probe_ats and look at it with "
                "sample_jobs. Propose only that company's board, at most one; if you cannot "
                "find it, propose nothing."
            )
        prefs = self.preferences
        prompt = (
            "Find up to "
            f"{self.target_proposals} job boards worth adding.\n"
            + preferences_summary(
                prefs.role_families,
                prefs.core_skills,
                prefs.preferred_cities or prefs.allowed_cities,
            )
        )
        if self.profile is not None:
            extra = profile_summary(self.profile.role_archetypes, self.profile.similar_companies)
            prompt += f"\n{extra}" if extra else ""
        return prompt

    def _invoke(self, deps: DiscoveryDeps) -> tuple[str, str]:
        """Run the loop. Every way it can end leaves the proposals it earned intact."""
        if self.recording is not None:
            self.recording.start()
        try:
            result = asyncio.run(self._run_agent(deps))
        except TimeoutError:
            self._finish_recording(None, "timeout")
            seconds = self.run_limits.max_seconds
            return "", f"wall-clock ceiling reached after {seconds:g}s"
        except UsageLimitExceeded as exc:
            self._finish_recording(None, "usage_limit")
            return "", f"step ceiling reached: {exc}"
        except BudgetExceeded as exc:
            self._finish_recording(None, "budget")
            return "", str(exc)
        except (AgentRunError, ModelAPIError, LLMCallError) as exc:
            self._finish_recording(None, "error")
            log.warning("discovery model call failed: %s", exc)
            return "", f"model call failed: {exc}"
        self._finish_recording(result, "ok")
        return result.output, ""

    def _finish_recording(self, result: AgentRunResult[str] | None, outcome: str) -> None:
        if self.recording is not None:
            self.recording.finish(result, outcome)

    async def _run_agent(self, deps: DiscoveryDeps) -> AgentRunResult[str]:
        call = self.agent.run(self._prompt(), deps=deps, usage_limits=usage_limits(self.limits))
        seconds = self.run_limits.max_seconds
        if seconds <= 0:
            return await call
        return await asyncio.wait_for(call, timeout=seconds)

    def _persist(self, proposals: list[SourceProposal]) -> tuple[int, int]:
        stored = duplicates = 0
        for proposal in proposals:
            row_id = self.store.insert_proposal(
                SourceProposal(
                    board_type=proposal.board_type,
                    slug=proposal.slug,
                    company=proposal.company,
                    evidence=proposal.evidence,
                    confidence=proposal.confidence,
                    job_count=proposal.job_count,
                    sample_titles=proposal.sample_titles,
                    run_id=self.run_id,
                ),
                self.now,
            )
            if row_id is None:
                duplicates += 1
            else:
                stored += 1
        return stored, duplicates


def build_run(
    model: Model | str,
    store: SourceCatalog,
    search: SearchProvider | None,
    preferences: Preferences,
    identity: OwnerIdentity,
    now: datetime,
    **options: object,
) -> DiscoverSourcesRun:
    """Compose a run, refusing early when the one paid dependency is missing.

    Without search the agent can only re-derive companies the store has already met,
    which is the opposite of what it exists for — so this fails before a model is
    loaded rather than producing a run that cannot succeed.
    """
    if search is None:
        raise SearchUnavailable(
            "source discovery needs a search provider — set TAVILY_API_KEY in .env"
        )
    return DiscoverSourcesRun(
        agent=build_agent(model, options.get("recording")),  # type: ignore[arg-type]
        store=store,
        search=search,
        preferences=preferences,
        identity=identity,
        now=now,
        **options,  # type: ignore[arg-type]
    )
