"""The feedback agent: a short chat that can change the view, the preferences or the search.

It acts only through five tools. It never lowers the score bar: when few jobs clear it, it
says so and offers the lower matches.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from career_copilot.agents.assistant import tools
from career_copilot.agents.assistant.tools import AssistantDeps
from career_copilot.agents.recording import AgentRecording, timed

SYSTEM_PROMPT = """You help one job seeker with their list of recommended jobs. You can do
five things, only through your tools:
- view_counts: see how many jobs are above the bar, and what the page shows now.
- set_view: change how many jobs the page lists, and whether lower matches are included.
- add_intent: add one line to what they are looking for (it triggers a new search).
- refresh_jobs: start a new job search.
- exclude_roles: never show roles with these words in the title (for example "manager"), and
  move the ones the page lists to the Recycle bin, where they can be restored.

Pick the tool from what they ask, and call it as your very first action:
- A number of jobs to show ("5 more", "show 20"): set_view. With no number at all ("show
  more"): view_counts, then ask "How many more?" and change nothing.
- Looking for more or different jobs ("find FDE roles at AI labs", "look for remote jobs"):
  add_intent with their wording, which also starts the search. "Look for new jobs" with no
  new wish: refresh_jobs.
- Removing, hiding or excluding roles ("remove manager roles", "no principal"): exclude_roles
  with the title words. add_intent is only for what they want more of.
"N more" means a limit of the page's current limit plus N; set include_below to true when
fewer jobs clear the bar than they want. If few jobs are above the bar, say so and offer the
lower matches; never try to lower the bar. A tool that answers "refused" is telling you a
rule. Only say a change happened if a tool just told you so; if you did not call a tool, you
changed nothing, so do not say you did. Reply in one or two plain sentences."""


@dataclass(frozen=True, slots=True)
class AssistantLimits:
    """Ceilings per message, enforced inside pydantic-ai and around the run."""

    max_requests: int = 8
    max_tool_calls: int = 6
    max_seconds: float = 60.0
    # While a search holds the same local model, each request queues behind it.
    busy_seconds: float = 120.0


def build_assistant(
    model: Model | str, recording: AgentRecording | None = None
) -> Agent[AssistantDeps, str]:
    agent = Agent(model, deps_type=AssistantDeps, output_type=str, system_prompt=SYSTEM_PROMPT)

    @agent.tool
    def view_counts(ctx: RunContext[AssistantDeps]) -> str:
        """How many jobs are above the bar, below it, and what the page shows now."""
        return timed(recording, "view_counts", tools.view_counts)(ctx.deps)

    @agent.tool
    def set_view(ctx: RunContext[AssistantDeps], limit: int, include_below: bool) -> str:
        """Show up to `limit` jobs (1 to 50); `include_below` adds lower matches."""
        return timed(recording, "set_view", tools.set_view)(ctx.deps, limit, include_below)

    @agent.tool
    def add_intent(ctx: RunContext[AssistantDeps], text: str) -> str:
        """Add one line, in the user's words, to what they are looking for."""
        return timed(recording, "add_intent", tools.add_intent)(ctx.deps, text)

    @agent.tool
    def exclude_roles(ctx: RunContext[AssistantDeps], terms: list[str]) -> str:
        """Exclude roles whose title has any of these words (at most 5, e.g. ["manager"])."""
        return timed(recording, "exclude_roles", tools.exclude_roles)(ctx.deps, terms)

    @agent.tool
    def refresh_jobs(ctx: RunContext[AssistantDeps]) -> str:
        """Start a new job search."""
        return timed(recording, "refresh_jobs", tools.refresh_jobs)(ctx.deps)

    return agent


def usage_limits(limits: AssistantLimits) -> UsageLimits:
    return UsageLimits(request_limit=limits.max_requests, tool_calls_limit=limits.max_tool_calls)
