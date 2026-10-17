"""Cheap first pass. Scores relevance so only the top N reach the expensive model."""

from __future__ import annotations

from dataclasses import dataclass

from career_copilot.agents import clamp, prompt_hash
from career_copilot.domain.models import JobPosting
from career_copilot.ports import LLMClient

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "relevance": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["relevance", "reason"],
}

SYSTEM = (
    "You triage job postings against a candidate's stated preferences. "
    "Score relevance 0-100 and give one short sentence of reasoning. "
    "Be strict: 100 means the posting matches the top-priority role type exactly."
)


@dataclass(frozen=True, slots=True)
class TriageResult:
    relevance: int
    reason: str
    prompt_hash: str
    model: str
    cost_eur: float
    calls: int = 1


class TriageAgent:
    """One cheap call per hard-filter survivor."""

    def __init__(self, client: LLMClient, preferences: str, description_chars: int = 1200) -> None:
        self._client = client
        self._preferences = preferences
        self._description_chars = description_chars

    def build_prompt(self, posting: JobPosting) -> str:
        return (
            f"PREFERENCES\n{self._preferences}\n\n"
            f"POSTING\n"
            f"Title: {posting.title}\n"
            f"Company: {posting.company}\n"
            f"Location: {posting.location or 'not stated'}\n"
            f"Description: {posting.description[: self._description_chars]}\n"
        )

    def evaluate(self, posting: JobPosting) -> TriageResult:
        prompt = self.build_prompt(posting)
        response = self._client.complete(prompt, SCHEMA, system=SYSTEM)
        return TriageResult(
            relevance=clamp(response.data.get("relevance")),
            reason=str(response.data.get("reason", "")),
            prompt_hash=prompt_hash(prompt),
            model=response.model,
            cost_eur=response.cost_eur,
            calls=response.calls,
        )
