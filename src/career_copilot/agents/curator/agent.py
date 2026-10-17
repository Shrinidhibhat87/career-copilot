"""The curator agent: picks the roles worth a match call from the embedding-ranked pool.

Embeddings rank by resemblance; the curator reads titles and descriptions against the
profile's must-haves and dealbreakers, which a cosine cannot. Its shortlist goes to the
match step first, and the embedding order fills whatever it leaves.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from career_copilot.agents.curator import tools
from career_copilot.agents.curator.tools import CuratorDeps
from career_copilot.agents.recording import AgentRecording, timed

SYSTEM_PROMPT = """You curate job postings for one job seeker. A ranking system has
already ordered a pool of postings by similarity to what they want; your job is to pick
the ones they would actually apply to.

Work in this order:
1. Read the profile below: the summary, must-haves and dealbreakers.
2. Call search_pool with short role phrases from the profile to see candidates.
3. Call get_job on a posting when its title alone does not settle it.
4. Call shortlist for each posting that fits, with a one-line reason. Skip postings that
   hit a dealbreaker or miss a must-have.

Be quick, every step costs seconds: the postings listed below can be shortlisted without
searching. Call search_pool at most 3 times, call get_job only when a title is unclear, and
call shortlist for every posting you pick in the same step instead of one per step. Only
shortlist ids you have been shown. A tool that answers "refused" is telling you a rule. When
the shortlist is full or searches stop turning up new fits, stop and reply with one sentence."""


@dataclass(frozen=True, slots=True)
class CuratorLimits:
    """Request and tool ceilings enforced inside pydantic-ai."""

    max_requests: int = 15
    max_tool_calls: int = 30
    max_seconds: float = 300.0


def build_curator(
    model: Model | str, recording: AgentRecording | None = None
) -> Agent[CuratorDeps, str]:
    agent = Agent(model, deps_type=CuratorDeps, output_type=str, system_prompt=SYSTEM_PROMPT)

    @agent.tool
    def search_pool(ctx: RunContext[CuratorDeps], query: str, k: int = 10) -> str:
        """Find pool postings closest to a short role phrase. Lines are id | title |
        company | location."""
        return timed(recording, "search_pool", tools.search_pool)(ctx.deps, query, k)

    @agent.tool
    def get_job(ctx: RunContext[CuratorDeps], job_id: int) -> str:
        """Read the start of one posting's description."""
        return timed(recording, "get_job", tools.get_job)(ctx.deps, job_id)

    @agent.tool
    def shortlist(ctx: RunContext[CuratorDeps], job_id: int, reason: str) -> str:
        """Add a posting the seeker would apply to, with a one-line reason."""
        return timed(recording, "shortlist", tools.shortlist)(ctx.deps, job_id, reason)

    return agent


def usage_limits(limits: CuratorLimits) -> UsageLimits:
    return UsageLimits(request_limit=limits.max_requests, tool_calls_limit=limits.max_tool_calls)


def curator_prompt(profile: str, deps: CuratorDeps, preview: int = 25) -> str:
    top = "\n".join(tools.line(job) for job in deps.pool[:preview])
    return (
        f"PROFILE\n{profile or '(no profile yet)'}\n\n"
        f"Shortlist up to {deps.limit} postings. The pool holds {len(deps.pool)}; "
        f"its top {min(preview, len(deps.pool))} by similarity are:\n{top}"
    )
