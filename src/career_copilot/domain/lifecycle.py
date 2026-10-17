"""What a user can decide about a posting, and how a review label maps onto it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# `restored` takes a job back out of the recycle bin. It counts as no decision: the job is
# listed and scored again exactly as if it had never been dismissed.
#
# `applied` keeps the job listed (with its badge) but never scores it again. Undoing it
# appends the decision that stood before, or `restored` when there was none.
DECISIONS: tuple[str, ...] = ("maybe", "dismissed", "saved", "restored", "applied")

# Why a job was dismissed. Optional: a dismissal without one still counts.
DISMISS_REASONS: tuple[str, ...] = ("wrong_role", "seniority", "location", "company", "other")

# A review label is feedback on the match; the decision is the control state ("is this
# hidden, is this my apply list"). One label implies one decision so there is a single
# answer to both questions.
LABEL_TO_DECISION: dict[str, str] = {
    "not_relevant": "dismissed",
    "good_fit": "saved",
    "maybe": "maybe",
}


@dataclass(frozen=True)
class BinItem:
    """A dismissed job still in the recycle bin, and how long it has left there."""

    job_id: int
    title: str
    company: str
    dismissed_at: datetime
    days_left: int
