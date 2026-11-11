"""Where an application stands after Applied, and how the board lays it out (spec 021)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

STAGES: tuple[str, ...] = ("applied", "interviewing", "offer", "rejected", "withdrawn")
COLUMNS: tuple[str, ...] = ("applied", "interviewing", "closed")
CLOSED: tuple[str, ...] = ("offer", "rejected", "withdrawn")
LABELS: dict[str, str] = {
    "applied": "Applied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
}
NOTE_LIMIT = 2000
DEFAULT_STALE_DAYS = 14
MAX_STALE_DAYS = 365


def column_of(stage: str) -> str:
    return stage if stage in ("applied", "interviewing") else "closed"


def days_quiet(last_change: datetime, now: datetime) -> int:
    return max(0, (now - last_change).days)


def is_stale(stage: str, last_change: datetime, now: datetime, stale_days: int) -> bool:
    """Only open applications go quiet; a closed one is finished."""
    return column_of(stage) != "closed" and days_quiet(last_change, now) >= stale_days


@dataclass(frozen=True)
class Board:
    applied: list[dict[str, object]]
    interviewing: list[dict[str, object]]
    offers: list[dict[str, object]]
    folded: list[dict[str, object]]  # rejected and withdrawn, behind the toggle
    hidden: list[dict[str, object]]


def arrange(cards: Sequence[dict[str, object]]) -> Board:
    """Hidden cards leave the columns; offers lead Closed; the rest is newest change first."""
    newest = sorted(cards, key=lambda c: str(c["lastChange"]), reverse=True)
    shown = [c for c in newest if not c["hidden"]]
    return Board(
        applied=[c for c in shown if c["stage"] == "applied"],
        interviewing=[c for c in shown if c["stage"] == "interviewing"],
        offers=[c for c in shown if c["stage"] == "offer"],
        folded=[c for c in shown if c["stage"] in ("rejected", "withdrawn")],
        hidden=[c for c in newest if c["hidden"]],
    )
