"""When a slot is due (spec 019). Pure."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from career_copilot.domain.schedule import due_slot, is_covered, next_slot, validate_times


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


def test_times_are_sorted_and_deduplicated() -> None:
    assert validate_times(["18:00", "08:00", "08:00"]) == ("08:00", "18:00")


@pytest.mark.parametrize("raw", [[], "08:00", ["8:00"], ["24:00"], ["08:60"], [8], ["0"] * 5])
def test_bad_times_are_refused(raw: object) -> None:
    with pytest.raises(ValueError):
        validate_times(raw)
