"""The note the match step reads: what the user saved, what they dismissed, and why."""

from __future__ import annotations

from datetime import datetime

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.preference_note import DecisionSignal, build_preference_note
from tests.conftest import make_raw


def test_no_decisions_means_no_note() -> None:
    assert build_preference_note([]) == ""


def test_the_note_lists_both_sides_and_counts_reasons() -> None:
    note = build_preference_note(
        [
            DecisionSignal("saved", "Forward Deployed Engineer", "Helsing"),
            DecisionSignal("dismissed", "Sales Manager", "Acme", "wrong_role"),
            DecisionSignal("dismissed", "Head of AI", "Globex", "seniority"),
            DecisionSignal("dismissed", "Account Executive", "Initech", "wrong_role"),
        ]
    )
    assert "Forward Deployed Engineer at Helsing" in note
    assert "Sales Manager at Acme [wrong role]" in note
    assert "wrong role (2), wrong seniority (1)" in note


def test_a_dismissal_without_a_reason_still_counts() -> None:
    note = build_preference_note([DecisionSignal("dismissed", "Sales Manager", "Acme")])
    assert "Sales Manager at Acme" in note
    assert "Why they dismissed" not in note


def test_the_note_is_bounded() -> None:
    many = [DecisionSignal("dismissed", f"Role {i}", "Acme") for i in range(50)]
    assert build_preference_note(many).count("- ") == 8


def test_the_store_reports_latest_choices_with_reasons(now: datetime) -> None:
    with SqliteStore(":memory:") as store:

        def put(key: str) -> int:
            posting = to_posting(
                make_raw(source_job_id=key, title=f"Role {key}", url=f"https://x.com/{key}"),
                store.company_aliases(),
            )
            return store.upsert_job(posting, apply_hard_filters(posting), now)[0]

        saved, dismissed, repost = put("a"), put("b"), put("c")
        store.record_decision(saved, "saved", "", now)
        store.record_decision(dismissed, "dismissed", "", now)
        store.record_decision(dismissed, "dismissed", "", now, "location")
        store.hide_as_repost(repost, dismissed, now)

        signals = {s.title: s for s in store.decision_signals()}

    assert set(signals) == {"Role a", "Role b"}  # the repost is not counted twice
    assert signals["Role a"].decision == "saved"
    assert signals["Role b"].reason == "location"
