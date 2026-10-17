"""The filter rules that preferences now drive: German level, and seniority bands."""

from __future__ import annotations

import pytest

from career_copilot.domain.filters import FilterConfig, apply_hard_filters
from career_copilot.domain.models import FilterOutcome, HardFilterResult
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.preferences import LanguageSkill, Preferences
from tests.conftest import make_raw


def result_for(config: FilterConfig, **overrides: object) -> HardFilterResult:
    if "description" in overrides:
        overrides["description"] = f"{overrides['description']} Python."
    return apply_hard_filters(to_posting(make_raw(**overrides)), config)


def with_german(level: str | None) -> FilterConfig:
    languages: tuple[LanguageSkill, ...] = (LanguageSkill("English", "C2"),)
    if level is not None:
        languages = (*languages, LanguageSkill("German", level))  # type: ignore[arg-type]
    return Preferences(
        allowed_cities=("berlin", "remote", ""),
        core_skills=("python",),
        languages=languages,
    ).to_filter_config()


# --- German, level-aware --------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "at_b1", "at_c1"),
    [
        ("You have German at C1 level.", FilterOutcome.REJECT, FilterOutcome.PASS),
        ("German at B2 is expected.", FilterOutcome.REJECT, FilterOutcome.PASS),
        ("Fluent German is required.", FilterOutcome.REJECT, FilterOutcome.PASS),
        ("Verhandlungssichere Deutschkenntnisse.", FilterOutcome.REJECT, FilterOutcome.PASS),
    ],
)
def test_a_german_demand_is_judged_against_the_stated_level(
    phrase: str, at_b1: FilterOutcome, at_c1: FilterOutcome
) -> None:
    assert result_for(with_german("B1"), description=phrase).outcome is at_b1
    assert result_for(with_german("C1"), description=phrase).outcome is at_c1


def test_a_demand_at_or_below_the_stated_level_still_warns() -> None:
    """Passing is not the same as silent: the demand is real and belongs in the digest."""
    result = result_for(with_german("C1"), description="You have German at B2 level.")
    assert result.outcome is FilterOutcome.PASS
    assert any("requires German at B2" in warning for warning in result.warnings)


@pytest.mark.parametrize("level", ["B1", "C1"])
def test_german_as_a_preference_only_warns_at_any_level(level: str) -> None:
    result = result_for(with_german(level), description="German is a plus.")
    assert result.outcome is FilterOutcome.PASS
    assert any("German preferred" in warning for warning in result.warnings)


def test_a_demand_for_an_unlisted_language_is_parked_for_review() -> None:
    """Never a silent pass: the preferences say nothing about German at all."""
    result = result_for(with_german(None), description="Fluent German is required.")
    assert result.outcome is FilterOutcome.MANUAL_REVIEW
    assert any("no German level stated" in reason for reason in result.reasons)


def test_raising_the_german_level_changes_which_postings_are_rejected() -> None:
    posting = {"description": "Fluent German is required."}
    assert result_for(with_german("B1"), **posting).outcome is FilterOutcome.REJECT
    assert result_for(with_german("C2"), **posting).outcome is FilterOutcome.PASS


# --- seniority bands ------------------------------------------------------


def with_bands(*bands: str) -> FilterConfig:
    return Preferences(
        allowed_cities=("berlin", "remote", ""),
        # The default posting in `make_raw` describes agentic AI work, so the stack rule
        # is satisfied and the seniority rule is the only thing under test.
        core_skills=("python", "agentic"),
        seniority=bands,  # type: ignore[arg-type]
        languages=(LanguageSkill("German", "B1"),),
    ).to_filter_config()


@pytest.mark.parametrize(
    ("title", "band"),
    [
        ("Junior Data Engineer", "junior"),
        ("Staff Machine Learning Engineer", "staff"),
        ("Head of Data Platform", "lead"),
    ],
)
def test_a_band_rule_fires_only_while_its_band_is_unselected(title: str, band: str) -> None:
    assert result_for(with_bands("mid", "senior"), title=title).outcome is FilterOutcome.REJECT
    assert result_for(with_bands("mid", "senior", band), title=title).outcome is FilterOutcome.PASS


def test_the_default_bands_reject_every_out_of_band_title() -> None:
    for title in ("Junior Data Engineer", "Principal Engineer", "VP Engineering"):
        assert result_for(FilterConfig(), title=title).outcome is FilterOutcome.REJECT


@pytest.mark.parametrize(
    ("title", "selected", "expected"),
    [
        ("Senior Data Engineer", "junior", FilterOutcome.REJECT),
        ("Data Engineer", "senior", FilterOutcome.REJECT),
        ("Senior Data Engineer", "senior", FilterOutcome.PASS),
        ("Data Engineer", "mid", FilterOutcome.PASS),
        ("Junior Data Engineer", "junior", FilterOutcome.PASS),
    ],
)
def test_selected_bands_exclude_other_levels(
    title: str, selected: str, expected: FilterOutcome
) -> None:
    assert result_for(with_bands(selected), title=title).outcome is expected


@pytest.mark.parametrize(
    "phrase", ["Native German required.", "German B2 minimum; German C2 required."]
)
def test_strongest_german_demand_is_not_hidden_by_weaker_wording(phrase: str) -> None:
    assert result_for(with_german("C1"), description=phrase).outcome is FilterOutcome.REJECT
