"""What a push carries and what a press does (spec 020). Pure."""

from __future__ import annotations

from career_copilot.domain.notifications import (
    CAP,
    decision_for,
    mask_chat,
    pick,
    shown_choice,
    telegram_text,
)
from career_copilot.domain.scoring import ScoredJob
from tests.unit.test_scoring import make_scored


def matches(n: int) -> list[ScoredJob]:
    return [make_scored(i, 90 - i) for i in range(1, n + 1)]


def test_pick_takes_the_best_five_and_counts_the_rest() -> None:
    chosen, more = pick(matches(8), set())
    assert [m.job.id for m in chosen] == [1, 2, 3, 4, 5] and more == 3 and CAP == 5


def test_pick_skips_jobs_already_sent() -> None:
    chosen, more = pick(matches(7), {1, 2})
    assert [m.job.id for m in chosen] == [3, 4, 5, 6, 7] and more == 0


def test_the_chat_is_masked_to_its_last_four_digits() -> None:
    assert mask_chat("123456784821") == "chat …4821"


def test_a_message_has_score_reasons_and_the_posting_link_only() -> None:
    text = telegram_text(matches(1)[0])
    assert text.startswith("89 — ") and "https://" in text and "localhost" not in text


def test_a_first_press_appends_the_choice() -> None:
    assert decision_for(None, "interested") == "saved"
    assert decision_for(None, "not_interested") == "dismissed"


def test_the_other_button_replaces_the_choice() -> None:
    assert decision_for("saved", "not_interested") == "dismissed"
    assert decision_for("dismissed", "interested") == "saved"


def test_pressing_the_chosen_button_again_takes_it_back() -> None:
    assert decision_for("saved", "interested") == "restored"
    assert decision_for("dismissed", "not_interested") == "restored"


def test_an_applied_job_is_left_alone() -> None:
    assert decision_for("applied", "not_interested") is None


def test_the_message_shows_the_choice_until_it_is_taken_back() -> None:
    assert shown_choice("saved", "interested") == "interested"
    assert shown_choice(None, "interested") is None
