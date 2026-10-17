"""What the user is looking for, in their own words, as an append-only log.

A single text field forgets: each edit overwrote the last idea. The log keeps every entry,
and the agent reads the active ones in order, so a newer entry can refine an older one.
Past `CONDENSE_AT_WORDS` the log is condensed into one note and the rest archived, which
keeps the agent's input short without deleting anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# One entry is a thought, not a document: a pasted CV belongs on the CV page.
MAX_ENTRY_CHARS = 2000
# Past this many active words the log is condensed, so the agent's input stays short.
CONDENSE_AT_WORDS = 1000
# A condensed note stands in for many entries, so it may run longer than one of them; the
# cap still keeps it a note and not the whole log pasted back in.
MAX_CONDENSED_CHARS = 4000


class IntentKind(StrEnum):
    """An entry the user wrote, or a note that condensed earlier entries."""

    ENTRY = "entry"
    CONDENSED = "condensed"


class NothingToCondenseError(Exception):
    """There are no active entries, so there is nothing to fold into a note."""


@dataclass(frozen=True, slots=True)
class IntentEntry:
    id: int
    text: str
    kind: IntentKind
    created_at: datetime


def active_words(entries: Sequence[IntentEntry]) -> int:
    """Whitespace-separated words, the unit `CONDENSE_AT_WORDS` is measured in."""
    return sum(len(entry.text.split()) for entry in entries)
