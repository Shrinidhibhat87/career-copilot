"""When a scheduled run is due (spec 019). Pure, no I/O: the same inputs give the same slot.

Slots are times of day in the timezone of the `now` passed in, so the caller decides whose
clock it is (the machine's local time in the app, UTC in tests).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta

MAX_TIMES = 4
_HH_MM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


@dataclass(frozen=True, slots=True)
class Schedule:
    version: int
    enabled: bool
    times: tuple[str, ...]  # "08:00": sorted, unique, at most MAX_TIMES
    saved_at: datetime


def validate_times(raw: object) -> tuple[str, ...]:
    """Sorted unique `HH:MM` strings. Raises ValueError with the message to show."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TIMES:
        raise ValueError(f"choose 1 to {MAX_TIMES} times")
    if not all(isinstance(t, str) and _HH_MM.fullmatch(t) for t in raw):
        raise ValueError("times look like 08:00")
    return tuple(sorted(set(raw)))


def _at(day: datetime, hhmm: str) -> datetime:
    return datetime.combine(day.date(), time.fromisoformat(hhmm), tzinfo=day.tzinfo)


def due_slot(times: Sequence[str], now: datetime) -> datetime | None:
    """The latest slot at or before now, looking back over today and yesterday."""
    slots = [_at(now - timedelta(days=d), t) for d in (0, 1) for t in times]
    return max((s for s in slots if s <= now), default=None)


def next_slot(times: Sequence[str], now: datetime) -> datetime | None:
    slots = [_at(now + timedelta(days=d), t) for d in (0, 1) for t in times]
    return min((s for s in slots if s > now), default=None)


def is_covered(slot: datetime, last_run_started: datetime | None) -> bool:
    """Any run that started at or after the slot covers it, whatever its outcome."""
    return last_run_started is not None and last_run_started >= slot
