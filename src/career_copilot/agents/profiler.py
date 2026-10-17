"""The profiler agent: turns the intent log, sample roles and CV into a search profile.

One structured answer, no tools. A 4B local model handles that reliably where it does
not handle a tool loop, and the curator, not the profiler, is the agent that explores.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from career_copilot.domain.sample_roles import SampleRole
from career_copilot.domain.search_profile import SearchProfile

SYSTEM_PROMPT = """You read what a job seeker wrote about the roles they want, postings
they liked, and their CV, and you write a search profile a job-ranking system will use.

Rules:
- The seeker's own words win. Newer intent entries refine older ones.
- A sample role is a positive example of the kind of work, not a demand for that company.
- Must-haves and dealbreakers come only from what the seeker wrote, never from the CV.
- Search phrases are short job-title-like phrases (2-6 words) a matching posting would use.
- Similar companies are real companies that hire for the same kind of role.
- Keep the summary to three or four sentences, in the third person."""

# A small local model has an 8k window; these keep the whole prompt well inside it.
MAX_CV_CHARS = 6000
MAX_ROLE_CHARS = 1200
MAX_ROLES = 6
# The summary is rendered into every scoring prompt, so it is paid for on every call.
MAX_SUMMARY_CHARS = 1200


class ProfileDraft(BaseModel):
    """What the model returns. Unbounded here and cut in `to_profile`: a 4B model that
    lists thirteen companies should be trimmed, not failed and retried."""

    summary: str
    role_archetypes: list[str] = Field(default_factory=list)
    must_haves: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    similar_companies: list[str] = Field(default_factory=list)
    search_phrases: list[str] = Field(default_factory=list)

    def to_profile(self) -> SearchProfile:
        def clean(values: list[str], limit: int = 8) -> tuple[str, ...]:
            unique = dict.fromkeys(v.strip()[:80] for v in values if v.strip())
            return tuple(unique)[:limit]

        return SearchProfile(
            summary=self.summary.strip()[:MAX_SUMMARY_CHARS],
            role_archetypes=clean(self.role_archetypes, 6),
            must_haves=clean(self.must_haves),
            dealbreakers=clean(self.dealbreakers),
            locations=clean(self.locations),
            seniority=clean(self.seniority, 4),
            similar_companies=clean(self.similar_companies, 12),
            search_phrases=clean(self.search_phrases, 10),
        )


def build_profiler(model: Model | str) -> Agent[None, ProfileDraft]:
    return Agent(model, output_type=ProfileDraft, system_prompt=SYSTEM_PROMPT, retries=2)


def profiler_prompt(intent: Sequence[str], roles: Sequence[SampleRole], cv: str) -> str:
    """The user message. Intent and CV arrive already redacted; postings are public text."""
    parts = ["WHAT THE SEEKER WROTE (oldest first)"]
    parts += [f"- {text}" for text in intent] or ["- (nothing yet)"]
    parts.append("\nSAMPLE ROLES THEY LIKED")
    for role in roles[:MAX_ROLES]:
        parts.append(f"## {role.title} at {role.company}\n{role.text[:MAX_ROLE_CHARS]}")
    if not roles:
        parts.append("(none)")
    parts.append(f"\nCV\n{cv[:MAX_CV_CHARS] or '(not uploaded)'}")
    return "\n".join(parts)
