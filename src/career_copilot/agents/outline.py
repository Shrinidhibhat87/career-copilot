"""Pulls a posting's responsibilities and requirements out of its text, word for word.

Boards open with company blurbs and name their sections differently ("Your tasks",
"We expect you to have"), so a model reads the text. It may only copy: every bullet is
checked against the posting and dropped when it is not in it, so a small model cannot
reword or invent a requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from career_copilot.domain.ad_text import clean_ad
from career_copilot.domain.models import JobPosting
from career_copilot.ports import LLMClient

# Sections sit deep in long postings, so this reads far more than triage's 1200 characters.
DESCRIPTION_CHARS = 12_000
MAX_BULLETS = 6
MAX_BULLET_CHARS = 220
# Below this a "bullet" is a heading or a fragment, not a sentence worth showing.
MIN_BULLET_CHARS = 15

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "requirements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["responsibilities", "requirements"],
}

SYSTEM = (
    "You pick out parts of a job posting. Copy sentences exactly as written, word for word; "
    "never reword, shorten or combine them. "
    "`responsibilities` are what the person will do in the role. "
    "`requirements` are what the candidate must bring (skills, experience, education). "
    f"Give at most {MAX_BULLETS} of each, the most important first. "
    "Skip the company introduction, benefits, perks, legal and equal-opportunity text. "
    "Use an empty list when the posting has no such part."
)


@dataclass(frozen=True, slots=True)
class Outline:
    responsibilities: tuple[str, ...]
    requirements: tuple[str, ...]
    model: str


class OutlineUnavailable(Exception):
    """The model answered, but nothing it returned is in the posting."""


def _squash(text: str) -> str:
    """Letters and digits only, so line breaks, bullets and glued words ("RoleYour") cannot
    make a verbatim quote look like a mismatch."""
    return re.sub(r"[\W_]+", "", text.casefold())


def _shorten(bullet: str) -> str:
    if len(bullet) <= MAX_BULLET_CHARS:
        return bullet
    return bullet[:MAX_BULLET_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def ground(bullets: object, description: str) -> tuple[str, ...]:
    """The bullets that appear in the description, trimmed, in order and without repeats."""
    if not isinstance(bullets, list):
        return ()
    haystack = _squash(description)
    kept: list[str] = []
    seen: set[str] = set()
    for item in bullets:
        text = " ".join(str(item).split())
        key = _squash(text)
        if len(key) < MIN_BULLET_CHARS or key in seen or key not in haystack:
            continue
        seen.add(key)
        kept.append(_shorten(text))
        if len(kept) == MAX_BULLETS:
            break
    return tuple(kept)


class OutlineAgent:
    """One call per posting, made when its card is first opened."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def build_prompt(self, posting: JobPosting) -> str:
        return (
            f"Title: {posting.title}\n"
            f"Company: {posting.company}\n\n"
            f"POSTING\n{clean_ad(posting.description)[:DESCRIPTION_CHARS]}\n"
        )

    def outline(self, posting: JobPosting) -> Outline:
        response = self._client.complete(self.build_prompt(posting), SCHEMA, system=SYSTEM)
        # Grounded against what the model was shown, not the whole text.
        shown = clean_ad(posting.description)[:DESCRIPTION_CHARS]
        responsibilities = ground(response.data.get("responsibilities"), shown)
        requirements = ground(response.data.get("requirements"), shown)
        if not responsibilities and not requirements:
            raise OutlineUnavailable("no returned sentence is in the posting")
        return Outline(responsibilities, requirements, response.model)
