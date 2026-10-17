"""The discovery agent: a pydantic-ai loop over five tools.

The loop belongs to the library; the control belongs here. Every stop condition is an
ordinary Python check inside a tool or in `UsageLimits`, so what ends a run is readable
without drawing a graph — which is why this is pydantic-ai and not a graph framework.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from career_copilot.agents.discovery import tools
from career_copilot.agents.discovery.tools import DiscoveryDeps
from career_copilot.agents.recording import AgentRecording, timed
from career_copilot.config import ResolvedRole

SYSTEM_PROMPT = """You find public job boards that a job seeker's crawler does not know
about yet.

Work in this order:
1. Call known_companies so you do not repeat what is already covered.
2. Search the web for companies that match the preferences below and are hiring in the
   named cities. Search for the company's board, not for the person.
3. For each candidate, guess its board slug and verify it with probe_ats. Guessing badly
   is free; proposing something unverified is not allowed.
4. Use sample_jobs on a board that exists to check it posts roles of the right kind in
   the right places.
5. Call propose_source only for boards you probed successfully, with evidence naming the
   search result and the job count.

A tool that answers "refused" is telling you a rule, not a retry hint. When you have the
proposals you were asked for, or a cap refuses you, stop and summarise what you found in
two or three sentences."""


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    """Request and tool ceilings enforced inside pydantic-ai."""

    max_requests: int = 12
    max_tool_calls: int = 24


def build_model(role: ResolvedRole) -> Model:
    """Any of the three backends, through one OpenAI-compatible provider.

    Ollama's OpenAI-compatible surface is at `/v1`, while its native API — the one the
    health probe reads — is not, so the suffix is added here rather than stored in
    config and used by both.
    """
    if role.kind == "ollama":
        base_url = f"{(role.base_url or 'http://localhost:11434').rstrip('/')}/v1"
        api_key = "local"
    elif role.kind == "vllm":
        base_url = (role.base_url or "http://localhost:8000/v1").rstrip("/")
        api_key = "local"
    else:
        # Imported here: the adapter pulls in `keyring`, which agent tests never need.
        from career_copilot.adapters.llm.backends import role_key

        base_url = role.base_url or "https://openrouter.ai/api/v1"
        api_key = role_key(role) or ""
    return OpenAIChatModel(role.model, provider=OpenAIProvider(base_url=base_url, api_key=api_key))


def build_agent(
    model: Model | str, recording: AgentRecording | None = None
) -> Agent[DiscoveryDeps, str]:
    """One agent, five tools. Tools take `deps` and nothing else that is stateful."""
    agent = Agent(model, deps_type=DiscoveryDeps, output_type=str, system_prompt=SYSTEM_PROMPT)

    @agent.tool
    def web_search(ctx: RunContext[DiscoveryDeps], query: str, max_results: int = 5) -> str:
        """Search the web. Costs money and is capped. Never search for a person."""
        return timed(recording, "web_search", tools.web_search)(ctx.deps, query, max_results)

    @agent.tool
    def probe_ats(ctx: RunContext[DiscoveryDeps], board_type: str, slug: str) -> str:
        """Check whether a board slug exists. board_type: greenhouse, lever, ashby,
        personio, teamtailor, smartrecruiters, recruitee or workable."""
        return timed(recording, "probe_ats", tools.probe_ats)(ctx.deps, board_type, slug)

    @agent.tool
    def sample_jobs(ctx: RunContext[DiscoveryDeps], board_type: str, slug: str, n: int = 5) -> str:
        """List titles and locations from a board you already probed."""
        return timed(recording, "sample_jobs", tools.sample_jobs)(ctx.deps, board_type, slug, n)

    @agent.tool
    def known_companies(ctx: RunContext[DiscoveryDeps]) -> str:
        """List boards and companies already known, so you do not repeat them."""
        return timed(recording, "known_companies", tools.known_companies)(ctx.deps)

    @agent.tool
    def propose_source(
        ctx: RunContext[DiscoveryDeps],
        board_type: str,
        slug: str,
        company: str,
        evidence: str,
        confidence: float = 0.5,
    ) -> str:
        """Propose a verified board for the user to review. Evidence is required."""
        return timed(recording, "propose_source", tools.propose_source)(
            ctx.deps, board_type, slug, company, evidence, confidence
        )

    return agent


def usage_limits(limits: DiscoveryLimits) -> UsageLimits:
    return UsageLimits(
        request_limit=limits.max_requests,
        tool_calls_limit=limits.max_tool_calls,
    )
