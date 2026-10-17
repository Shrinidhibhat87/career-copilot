"""The intent log's pure rules: how many words the active entries add up to."""

from __future__ import annotations

from datetime import UTC, datetime

from career_copilot.domain.intent import IntentEntry, IntentKind, active_words

T = datetime(2026, 10, 7, tzinfo=UTC)


def test_active_words_counts_whitespace_separated_words_across_entries() -> None:
    entries = [
        IntentEntry(1, "forward deployed engineer", IntentKind.ENTRY, T),
        IntentEntry(2, "  Berlin,  applied AI ", IntentKind.CONDENSED, T),
    ]
    assert active_words(entries) == 6


def test_active_words_of_nothing_is_zero() -> None:
    assert active_words([]) == 0
