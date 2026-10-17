"""Lift free-text notes into proposed structured changes. One model call, no writes.

Prose holds nuance a schema cannot ("Munich only if there is a Berlin office"), and today
that nuance is re-read from scratch on every pass. This pass proposes the structured edits
the note implies; the user accepts them one at a time in the screen. Nothing here writes,
and nothing is applied automatically, because a silent edit here would silently change
deterministic filtering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from career_copilot.domain.preferences import (
    CEFR_LEVELS,
    CHANGEABLE_FIELDS,
    SENIORITY_BANDS,
    FieldChange,
    Preferences,
    is_applicable,
    normalize_term,
)
from career_copilot.ports import LLMClient

log = logging.getLogger(__name__)

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(CHANGEABLE_FIELDS)},
                    "action": {"type": "string", "enum": ["add", "remove", "set"]},
                    "value": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["field", "action", "value", "rationale"],
            },
        }
    },
    "required": ["changes"],
}

SYSTEM = (
    "You turn a job-seeker's free-text notes into proposed edits to their structured "
    "preferences. Propose a change only where the note states it. Never invent a "
    "preference the note does not express, and never restate something already set. "
    "Values are lowercase match terms that must appear in a job posting verbatim: "
    "'berlin', not 'Berlin area'. A language value is 'name:LEVEL' with LEVEL one of "
    f"{', '.join(CEFR_LEVELS)}. A seniority value is one of {', '.join(SENIORITY_BANDS)}. "
    "A boolean value is 'true' or 'false'. Each change carries one short rationale "
    "quoting the part of the note it came from."
)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """The proposals and what asking for them cost."""

    changes: tuple[FieldChange, ...]
    cost_eur: float = 0.0


def build_prompt(notes: str, current: Preferences) -> str:
    return (
        f"CURRENT PREFERENCES\n{current.to_prompt_block()}\n"
        f"NOTES\n{notes.strip()}\n\n"
        "Propose the structured changes these notes imply. Return an empty list if none.\n"
    )


def _as_changes(data: object) -> tuple[FieldChange, ...]:
    if not isinstance(data, list):
        return ()
    changes: list[FieldChange] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        action = item.get("action")
        value = item.get("value")
        if not isinstance(field, str) or not isinstance(action, str) or not isinstance(value, str):
            continue
        changes.append(
            FieldChange(
                field=field,
                action=action,  # type: ignore[arg-type]
                value=(
                    ":".join(part.strip() for part in value.split(":"))
                    if field == "languages"
                    else normalize_term(value)
                ),
                rationale=str(item.get("rationale", "")),
            )
        )
    return tuple(changes)


def propose_from_notes(notes: str, current: Preferences, client: LLMClient) -> ExtractionResult:
    """Ask the model what the notes imply. Returns only changes a save would accept.

    A proposal naming an unknown field, or carrying a value the validators reject, is
    dropped rather than surfaced: a hostile or confused suggestion must not reach a screen
    whose buttons write to deterministic filtering.
    """
    if not notes.strip():
        return ExtractionResult(changes=())
    response = client.complete(build_prompt(notes, current), SCHEMA, system=SYSTEM)
    proposed = _as_changes(response.data.get("changes"))
    kept = tuple(change for change in proposed if is_applicable(current, change))
    if len(kept) != len(proposed):
        log.info("dropped %d unusable preference proposal(s)", len(proposed) - len(kept))
    return ExtractionResult(changes=kept, cost_eur=response.cost_eur)
