"""When a scheduled run is due (spec 019). Pure, no I/O: the same inputs give the same slot.

Slots are times of day in the timezone of the `now` passed in, so the caller decides whose
clock it is (the machine's local time in the app, UTC in tests).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta

_HALF_HOUR = re.compile(r"([01]\d|2[0-3]):(00|30)")


@dataclass(frozen=True, slots=True)
class Slot:
    time: str  # "08:00", on the hour or half hour
    on: bool


# Four a day, named by position in the app (Morning, Midday, Evening, Night). An off slot
# keeps its time, so switching it back on does not lose the choice.
DEFAULT_SLOTS = (
    Slot("08:00", True),
    Slot("12:30", False),
    Slot("18:00", True),
    Slot("22:00", False),
)


@dataclass(frozen=True, slots=True)
class Schedule:
    version: int
    enabled: bool
    slots: tuple[Slot, ...]
    saved_at: datetime

    @property
    def times(self) -> tuple[str, ...]:
        """The switched-on slots' times, sorted."""
        return tuple(sorted(s.time for s in self.slots if s.on))


def slots_from_times(times: Sequence[str]) -> tuple[Slot, ...]:
    """Slots for a saved list of times: those first, switched on, then the unused defaults off."""
    return tuple(Slot(t, True) for t in times) + tuple(
        Slot(s.time, False) for s in DEFAULT_SLOTS[len(times) :]
    )


def validate_slots(raw: object) -> tuple[Slot, ...]:
    """Exactly four slots on the hour or half hour, no two on at the same time.
    Raises ValueError with the message to show."""
    if not isinstance(raw, list) or len(raw) != len(DEFAULT_SLOTS):
        raise ValueError("choose four slots")
    slots: list[Slot] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("on"), bool):
            raise ValueError("choose four slots")
        time = item.get("time")
        if not isinstance(time, str) or not _HALF_HOUR.fullmatch(time):
            raise ValueError("times are on the hour or half hour")
        slots.append(Slot(time, item["on"]))
    on = [s.time for s in slots if s.on]
    if len(on) != len(set(on)):
        raise ValueError("two slots have the same time")
    return tuple(slots)


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
