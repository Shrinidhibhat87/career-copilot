"""Notes to proposals: one model call, nothing written, nothing applied."""

from __future__ import annotations

from typing import Any

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.domain.preferences import LanguageSkill, Preferences
from career_copilot.usecases.extract_preferences import propose_from_notes

CURRENT = Preferences(
    core_skills=("python",),
    allowed_cities=("berlin", "remote"),
    flagged_cities=("munich",),
    languages=(LanguageSkill("German", "B1"),),
)


def change(**overrides: object) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "field": "flagged_cities",
        "action": "add",
        "value": "hamburg",
        "rationale": "the note names Hamburg",
    }
    proposal.update(overrides)
    return proposal


def llm(*changes: dict[str, Any], cost: float = 0.01) -> FakeLLM:
    return FakeLLM(default={"changes": list(changes)}, cost_per_call_eur=cost)


def test_a_proposal_is_returned_with_its_rationale_and_cost() -> None:
    result = propose_from_notes("Hamburg is fine too.", CURRENT, llm(change()))
    assert [(c.field, c.action, c.value) for c in result.changes] == [
        ("flagged_cities", "add", "hamburg")
    ]
    assert result.changes[0].rationale == "the note names Hamburg"
    assert result.cost_eur == 0.01


def test_the_prompt_carries_the_current_preferences_and_the_notes() -> None:
    client = llm(change())
    propose_from_notes("Hamburg is fine too.", CURRENT, client)
    prompt = client.calls[0]
    assert "Hamburg is fine too." in prompt
    assert "Flagged cities: munich" in prompt


def test_an_empty_note_never_reaches_the_model() -> None:
    client = llm(change())
    result = propose_from_notes("   ", CURRENT, client)
    assert result.changes == ()
    assert client.calls == []


def test_a_proposal_naming_an_unknown_field_is_dropped() -> None:
    result = propose_from_notes("pay me more", CURRENT, llm(change(field="salary")))
    assert result.changes == ()


def test_a_proposal_with_an_invalid_cefr_level_is_dropped() -> None:
    proposal = change(field="languages", action="add", value="German:B7")
    assert propose_from_notes("my German improved", CURRENT, llm(proposal)).changes == ()


def test_a_proposal_that_would_leave_invalid_preferences_is_dropped() -> None:
    """Removing the last city is applicable but unsaveable, so it never reaches the form."""
    prefs = Preferences(allowed_cities=("berlin",), languages=CURRENT.languages)
    proposal = change(field="allowed_cities", action="remove", value="berlin")
    assert propose_from_notes("not Berlin", prefs, llm(proposal)).changes == ()


def test_usable_proposals_survive_alongside_dropped_ones() -> None:
    result = propose_from_notes(
        "Hamburg is fine, pay me more",
        CURRENT,
        llm(change(), change(field="salary")),
    )
    assert [c.value for c in result.changes] == ["hamburg"]


def test_nothing_is_applied_to_the_current_preferences() -> None:
    before = CURRENT.to_payload()
    propose_from_notes("Hamburg is fine too.", CURRENT, llm(change()))
    assert CURRENT.to_payload() == before
