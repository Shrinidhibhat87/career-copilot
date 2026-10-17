"""The search profile: what the profiler agent understood the user to want.

Built from the active intent log, the sample roles and the CV, and cached by a hash of
those inputs, so an unchanged log never pays for a second profile. Pure: no I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from career_copilot.domain.intent import IntentEntry
from career_copilot.domain.sample_roles import SampleRole

# The list fields, in the order they render. `summary` is the one free-text field.
LIST_FIELDS: tuple[str, ...] = (
    "role_archetypes",
    "must_haves",
    "dealbreakers",
    "locations",
    "seniority",
    "similar_companies",
    "search_phrases",
)


@dataclass(frozen=True, slots=True)
class SearchProfile:
    summary: str
    role_archetypes: tuple[str, ...] = ()
    must_haves: tuple[str, ...] = ()
    dealbreakers: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    seniority: tuple[str, ...] = ()
    similar_companies: tuple[str, ...] = ()
    search_phrases: tuple[str, ...] = ()

    def to_prompt_text(self) -> str:
        """What the scoring prompts read in place of the old `role_intent` field.

        Similar companies are left out on purpose: they steer discovery, and naming them
        to a scoring model would bias it towards a company rather than the role.
        """
        lines = [self.summary.strip()]
        for name in LIST_FIELDS:
            values: tuple[str, ...] = getattr(self, name)
            if values and name != "similar_companies":
                lines.append(f"{name.replace('_', ' ').capitalize()}: {', '.join(values)}")
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"summary": self.summary}
        for name in LIST_FIELDS:
            payload[name] = list(getattr(self, name))
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SearchProfile:
        def items(key: str) -> tuple[str, ...]:
            value = payload.get(key, ())
            return tuple(str(v) for v in value) if isinstance(value, list) else ()

        return cls(str(payload.get("summary", "")), *(items(name) for name in LIST_FIELDS))


def inputs_hash(
    entries: Sequence[IntentEntry], roles: Sequence[SampleRole], cv_sha256: str | None
) -> str:
    """Identity of what the profiler read. Ids are excluded: a re-added text is the same input."""
    digest = hashlib.sha256()
    for part in (
        *(f"intent\0{e.kind}\0{e.text}" for e in entries),
        *(f"role\0{r.company}\0{r.title}\0{r.text}" for r in roles),
        f"cv\0{cv_sha256 or ''}",
    ):
        digest.update(part.encode())
        digest.update(b"\x1e")
    return digest.hexdigest()[:32]
