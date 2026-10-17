"""The condenser agent: folds the active intent log into one note in the user's voice.

Plain text out, no tools. The note replaces the entries it was drawn from once the user
approves it, so the prompt asks for every concrete requirement to survive, not a summary.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai import Agent
from pydantic_ai.models import Model

SYSTEM_PROMPT = """You rewrite a job seeker's notes about the roles they want into one note.

Rules:
- The notes are oldest first. A newer note refines or overrides an older one; when they
  conflict, keep the newer wish and drop the older.
- Keep every concrete requirement: roles, seniority, locations, remote or on-site,
  must-haves, dealbreakers, industries, and companies they liked or ruled out.
- Drop repetition and filler. Do not add anything the seeker did not write.
- Write in the first person, as the seeker, in plain prose. Under 250 words.
- Placeholders such as <|PERSON_1|> stand for private details; copy them exactly.
- Reply with the note only: no heading, no preamble, no list of changes."""


def build_condenser(model: Model | str) -> Agent[None, str]:
    return Agent(model, output_type=str, system_prompt=SYSTEM_PROMPT)


def condense_prompt(texts: Sequence[str]) -> str:
    """The user message. The entries arrive already redacted, numbered oldest first."""
    parts = ["THE SEEKER'S NOTES (oldest first)"]
    parts += [f"{number}. {text.strip()}" for number, text in enumerate(texts, start=1)]
    parts.append("\nWrite the single condensed note.")
    return "\n".join(parts)
