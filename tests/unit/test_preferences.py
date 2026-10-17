"""The preference model: validation, both derivations, and hash identity."""

from __future__ import annotations

from dataclasses import replace

import pytest

from career_copilot.domain.filters import FilterConfig
from career_copilot.domain.preferences import (
    DEFAULT_TITLE_TERMS,
    MAX_ROLE_INTENT_CHARS,
    FieldChange,
    LanguageSkill,
    Preferences,
    PreferencesError,
    apply_change,
    bootstrap,
    is_applicable,
    normalize_term,
    validate,
)


def make(**overrides: object) -> Preferences:
    """A complete, valid set of preferences."""
    defaults: dict[str, object] = {
        "role_families": ("ai/ml engineering",),
        "title_terms": ("data engineer",),
        "core_skills": ("python", "kafka"),
        "excluded_stack": ("java",),
        "excluded_role_terms": ("frontend",),
        "seniority": ("mid", "senior"),
        "allowed_cities": ("berlin", "remote"),
        "preferred_cities": ("berlin",),
        "flagged_cities": ("munich",),
        "languages": (LanguageSkill("English", "C2"), LanguageSkill("German", "B1")),
        "notes": "Munich only with a Berlin office.",
    }
    defaults.update(overrides)
    return Preferences(**defaults)  # type: ignore[arg-type]


# --- validation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"role_families": tuple(f"role{n}" for n in range(6))}, "role_families"),
        ({"allowed_cities": ()}, "allowed_cities"),
        ({"seniority": ()}, "seniority"),
        ({"seniority": ("principal",)}, "seniority"),
        ({"languages": (LanguageSkill("German", "B7"),)}, "languages"),  # type: ignore[arg-type]
        (
            {"languages": (LanguageSkill("German", "B1"), LanguageSkill("german", "B2"))},
            "languages",
        ),
        ({"batch_size": 0}, "batch_size"),
        ({"max_per_company": 0}, "max_per_company"),
    ],
)
def test_invalid_preferences_name_the_offending_field(
    overrides: dict[str, object], field: str
) -> None:
    with pytest.raises(PreferencesError) as error:
        validate(make(**overrides))
    assert field in error.value.errors


def test_five_role_families_are_allowed() -> None:
    validate(make(role_families=tuple(f"role{n}" for n in range(5))))


def test_a_hand_typed_skill_becomes_a_term() -> None:
    assert normalize_term("  Apache   Kafka ") == "apache kafka"


# --- derivations ----------------------------------------------------------


def test_to_filter_config_carries_every_rule_input() -> None:
    config = make().to_filter_config()
    assert config.core_stack_terms == ("python", "kafka")
    assert config.excluded_stack_terms == ("java",)
    assert config.allowed_cities == frozenset({"berlin", "remote"})
    assert config.flagged_cities == frozenset({"munich"})
    assert config.seniority_bands == ("mid", "senior")
    assert dict(config.language_levels)["german"] == "B1"


def test_the_prompt_block_carries_notes_verbatim() -> None:
    block = make(notes="Munich only if there is a Berlin office.").to_prompt_block()
    assert "Munich only if there is a Berlin office." in block
    assert "Core skills: python, kafka" in block
    assert "Languages: English C2, German B1" in block


def test_the_prompt_block_is_deterministic() -> None:
    assert make().to_prompt_block() == make().to_prompt_block()


def test_the_prompt_block_carries_the_role_intent_verbatim() -> None:
    sentence = "I am looking for forward-deployed engineering roles in Berlin."
    assert sentence in make(role_intent=sentence).to_prompt_block()


def test_an_empty_role_intent_renders_nothing_and_keeps_the_hash() -> None:
    """The guard on every score already paid for. `role_intent` arrived after those rows
    were written, so an unstated one has to render byte-identically to no field at all."""
    prefs = make()
    assert "Looking for:" not in prefs.to_prompt_block()
    assert replace(prefs, role_intent="   ").prompt_hash() == prefs.prompt_hash()


# --- hash identity --------------------------------------------------------


@pytest.mark.parametrize("field", ["max_per_company", "batch_size"])
def test_operational_fields_stay_out_of_the_hash(field: str) -> None:
    prefs = make()
    updates: dict[str, object] = {field: 9}
    changed = replace(prefs, **updates)  # type: ignore[arg-type]
    assert changed.prompt_hash() == prefs.prompt_hash()


@pytest.mark.parametrize(
    "overrides",
    [
        {"core_skills": ("python",)},
        {"notes": "something else"},
        {"languages": (LanguageSkill("German", "C1"),)},
        {"seniority": ("mid",)},
        {"allowed_cities": ("berlin",)},
        {"allow_remote": False},
        {"require_permanent": False},
        {"role_families": ("backend",)},
        {"excluded_stack": ()},
        {"excluded_role_terms": ()},
        {"preferred_cities": ()},
        {"flagged_cities": ()},
        {"role_intent": "I want forward-deployed engineering roles"},
    ],
)
def test_every_prompt_field_changes_the_hash(overrides: dict[str, object]) -> None:
    assert make(**overrides).prompt_hash() != make().prompt_hash()


def test_version_and_timestamp_are_not_part_of_the_hash() -> None:
    prefs = make()
    assert replace(prefs, version=7).prompt_hash() == prefs.prompt_hash()


def test_the_title_gate_is_not_part_of_the_hash() -> None:
    """A matcher list the model never sees must not invalidate scores already paid for."""
    prefs = make()
    assert replace(prefs, title_terms=("sre", "mlops")).prompt_hash() == prefs.prompt_hash()


# --- serialization --------------------------------------------------------


def test_an_absent_title_list_is_seeded_not_read_as_no_gate() -> None:
    """Rows written before the role gate existed never stated a title list. Reading that
    as "the user wants no gate" would leave every earlier install unfiltered by title."""
    payload = make().to_payload()
    del payload["title_terms"]
    assert Preferences.from_payload(payload).title_terms == DEFAULT_TITLE_TERMS


def test_a_title_list_saved_as_empty_stays_empty() -> None:
    payload = make(title_terms=()).to_payload()
    assert Preferences.from_payload(payload).title_terms == ()


def test_payload_round_trips() -> None:
    prefs = make()
    assert Preferences.from_payload(prefs.to_payload()).prompt_hash() == prefs.prompt_hash()


def test_a_payload_written_before_a_field_existed_still_loads() -> None:
    payload = make().to_payload()
    del payload["batch_size"]
    assert Preferences.from_payload(payload).batch_size == 6


def test_a_payload_written_before_the_role_intent_loads_unstated() -> None:
    payload = make().to_payload()
    del payload["role_intent"]
    assert Preferences.from_payload(payload).role_intent == ""


def test_a_role_intent_round_trips() -> None:
    prefs = make(role_intent="FDE roles, ideally customer-facing")
    assert Preferences.from_payload(prefs.to_payload()).role_intent == prefs.role_intent


def test_an_over_long_role_intent_is_refused() -> None:
    with pytest.raises(PreferencesError) as raised:
        validate(make(role_intent="x" * (MAX_ROLE_INTENT_CHARS + 1)))
    assert "role_intent" in raised.value.errors


# --- bootstrap ------------------------------------------------------------


def test_bootstrap_is_version_zero_and_carries_the_notes() -> None:
    prefs = bootstrap(FilterConfig(), notes="prose", batch_size=6)
    assert prefs.version == 0
    assert prefs.notes == "prose"
    assert prefs.languages == (LanguageSkill("English", "C2"), LanguageSkill("German", "B1"))


def test_bootstrap_reproduces_the_seed_filter_config() -> None:
    seed = FilterConfig()
    derived = bootstrap(seed, notes="", batch_size=6).to_filter_config()
    assert derived == seed


# --- proposed changes -----------------------------------------------------


def test_a_change_adds_one_term_without_touching_the_rest() -> None:
    updated = apply_change(make(), FieldChange("flagged_cities", "add", "Hamburg"))
    assert updated.flagged_cities == ("munich", "hamburg")
    assert updated.core_skills == make().core_skills


def test_adding_a_language_already_listed_changes_its_level() -> None:
    updated = apply_change(make(), FieldChange("languages", "add", "German:B2"))
    assert updated.language_level("german") == "B2"
    assert len(updated.languages) == 2


def test_a_boolean_change_is_a_set() -> None:
    assert apply_change(make(), FieldChange("allow_remote", "set", "false")).allow_remote is False


@pytest.mark.parametrize(
    "change",
    [
        FieldChange("salary", "add", "100000"),
        FieldChange("core_skills", "set", "python"),
        FieldChange("allow_remote", "add", "true"),
        FieldChange("batch_size", "set", "many"),
        FieldChange("languages", "add", "German:B7"),
    ],
)
def test_an_unusable_change_is_refused(change: FieldChange) -> None:
    with pytest.raises(PreferencesError):
        apply_change(make(), change)
    assert is_applicable(make(), change) is False


def test_a_change_that_would_invalidate_the_result_is_not_applicable() -> None:
    """Emptying the city list applies cleanly but leaves preferences a save would refuse."""
    change = FieldChange("allowed_cities", "remove", "berlin")
    prefs = make(allowed_cities=("berlin",))
    assert apply_change(prefs, change).allowed_cities == ()
    assert is_applicable(prefs, change) is False
