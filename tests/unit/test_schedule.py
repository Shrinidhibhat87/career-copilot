"""When a slot is due (spec 019). Pure."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from career_copilot.domain.schedule import (
    DEFAULT_SLOTS,
    Schedule,
    Slot,
    due_slot,
    is_covered,
    next_slot,
    slots_from_times,
    validate_slots,
)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2030, 1, day, hour, minute, tzinfo=UTC)


def test_the_latest_slot_at_or_before_now_is_due() -> None:
    assert due_slot(["08:00", "18:00"], at(10, 19)) == at(10, 18)
    assert due_slot(["08:00", "18:00"], at(10, 8)) == at(10, 8)


def test_before_the_first_slot_of_the_day_yesterdays_last_is_due() -> None:
    assert due_slot(["08:00", "18:00"], at(10, 7, 59)) == at(9, 18)


def test_no_times_means_nothing_is_due_or_next() -> None:
    assert due_slot([], at(10, 12)) is None
    assert next_slot([], at(10, 12)) is None


def test_next_slot_rolls_to_tomorrow() -> None:
    assert next_slot(["08:00", "18:00"], at(10, 19)) == at(11, 8)
    assert next_slot(["08:00", "18:00"], at(10, 8)) == at(10, 18)


def test_a_run_covers_a_slot_from_the_slot_onward() -> None:
    assert is_covered(at(10, 8), at(10, 8))
    assert is_covered(at(10, 8), at(10, 9))
    assert not is_covered(at(10, 8), at(10, 7, 59))
    assert not is_covered(at(10, 8), None)


def body(*times: str, on: bool = True) -> list[dict[str, object]]:
    return [{"time": t, "on": on} for t in times]


def test_four_slots_on_the_half_hour_are_accepted() -> None:
    slots = validate_slots(body("08:00", "09:30", "18:00", "22:30"))
    assert [s.time for s in slots] == ["08:00", "09:30", "18:00", "22:30"]


@pytest.mark.parametrize(
    "raw",
    [
        [],
        "08:00",
        body("08:00", "09:00", "10:00"),
        body("08:00", "09:00", "10:00", "11:00", "12:00"),
        [{"time": "08:00"}] * 4,
        [{"time": "08:00", "on": "yes"}] * 4,
    ],
)
def test_the_wrong_number_or_shape_of_slots_is_refused(raw: object) -> None:
    with pytest.raises(ValueError, match="four slots"):
        validate_slots(raw)


@pytest.mark.parametrize("bad", ["10:01", "10:15", "8:00", "24:00", "10:45"])
def test_only_the_hour_and_half_hour_are_allowed(bad: str) -> None:
    with pytest.raises(ValueError, match="hour or half hour"):
        validate_slots(body("08:00", "09:00", "10:00", bad))


def test_two_switched_on_slots_cannot_share_a_time() -> None:
    with pytest.raises(ValueError, match="same time"):
        validate_slots(body("08:00", "08:00", "10:00", "11:00"))


def test_an_off_slot_may_share_a_time_and_keeps_it() -> None:
    raw = [{"time": "08:00", "on": True}, {"time": "08:00", "on": False}, *body("10:00", "11:00")]
    assert validate_slots(raw)[1] == Slot("08:00", False)


def test_times_are_the_switched_on_ones_sorted() -> None:
    schedule = Schedule(1, True, DEFAULT_SLOTS, at(10, 0))
    assert schedule.times == ("08:00", "18:00")


def test_old_saved_times_become_on_slots_followed_by_unused_defaults() -> None:
    slots = slots_from_times(("07:00", "19:00"))
    assert slots[:2] == (Slot("07:00", True), Slot("19:00", True))
    assert slots[2:] == (Slot("18:00", False), Slot("22:00", False))
