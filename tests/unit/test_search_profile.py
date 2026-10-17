"""The search profile as data: rendering, round trip, cache identity, trimming."""

from __future__ import annotations

from datetime import UTC, datetime

from career_copilot.agents.profiler import ProfileDraft, profiler_prompt
from career_copilot.domain.intent import IntentEntry, IntentKind
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.sample_roles import SampleRole
from career_copilot.domain.search_profile import SearchProfile, inputs_hash

T = datetime(2026, 10, 7, tzinfo=UTC)
PROFILE = SearchProfile(
    summary="Wants forward-deployed engineering in applied AI.",
    role_archetypes=("forward deployed engineer",),
    similar_companies=("Taktile",),
    search_phrases=("forward deployed engineer", "solutions engineer"),
)


def entry(text: str, id: int = 1) -> IntentEntry:
    return IntentEntry(id, text, IntentKind.ENTRY, T)


def role(text: str = "Build decision engines with customers.") -> SampleRole:
    return SampleRole(1, "ashby", "taktile", "594e", "Taktile", "FDE", text, None, T)


def test_prompt_text_leaves_out_similar_companies() -> None:
    text = PROFILE.to_prompt_text()
    assert text.startswith("Wants forward-deployed engineering")
    assert "Role archetypes: forward deployed engineer" in text
    assert "Taktile" not in text


def test_payload_round_trips() -> None:
    assert SearchProfile.from_payload(PROFILE.to_payload()) == PROFILE


def test_inputs_hash_ignores_ids_but_not_text_or_cv() -> None:
    base = inputs_hash([entry("FDE roles")], [role()], "cv1")
    assert inputs_hash([entry("FDE roles", id=9)], [role()], "cv1") == base
    assert inputs_hash([entry("FDE roles, Berlin")], [role()], "cv1") != base
    assert inputs_hash([entry("FDE roles")], [role("other text")], "cv1") != base
    assert inputs_hash([entry("FDE roles")], [role()], "cv2") != base


def test_draft_is_trimmed_and_deduplicated_not_rejected() -> None:
    draft = ProfileDraft(
        summary="  x  ",
        similar_companies=[f"Co {i}" for i in range(20)],
        search_phrases=["a", "a", " ", "b"],
    )
    profile = draft.to_profile()
    assert profile.summary == "x"
    assert len(profile.similar_companies) == 12
    assert profile.search_phrases == ("a", "b")


def test_profile_replaces_role_intent_in_the_prompt_and_its_hash() -> None:
    prefs = Preferences(role_intent="old single field")
    profiled = Preferences(role_intent="old single field", search_profile="the profile")
    assert "old single field" in prefs.to_prompt_block()
    assert "the profile" in profiled.to_prompt_block()
    assert "old single field" not in profiled.to_prompt_block()
    assert prefs.prompt_hash() != profiled.prompt_hash()


def test_prompt_bounds_roles_and_cv() -> None:
    prompt = profiler_prompt(["FDE roles"], [role("x" * 5000)], "c" * 20000)
    assert "- FDE roles" in prompt
    assert "## FDE at Taktile" in prompt
    assert len(prompt) < 8000
