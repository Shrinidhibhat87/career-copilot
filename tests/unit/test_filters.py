"""Hard filters must match profile/preferences.md exactly."""

from __future__ import annotations

import pytest

from career_copilot.domain.filters import (
    FilterConfig,
    _match_text,
    _term_hits,
    apply_hard_filters,
    term_pattern,
)
from career_copilot.domain.models import FilterOutcome
from career_copilot.domain.normalize import to_posting
from tests.conftest import make_raw


def outcome_for(**overrides: object) -> FilterOutcome:
    if "description" in overrides:
        overrides["description"] = f"{overrides['description']} Python."
    return apply_hard_filters(to_posting(make_raw(**overrides))).outcome


def test_clean_berlin_role_passes() -> None:
    assert outcome_for() is FilterOutcome.PASS


# --- German language ------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "Fluent German is required.",
        "Business fluent German (C1) expected.",
        "Verhandlungssichere Deutschkenntnisse.",
        "You have German at B2 level.",
        "Fluency in German and English.",
        "German is mandatory for this role.",
    ],
)
def test_working_proficiency_german_is_rejected(phrase: str) -> None:
    assert outcome_for(description=phrase) is FilterOutcome.REJECT


@pytest.mark.parametrize(
    "phrase",
    [
        "German is a plus.",
        "German language skills are nice-to-have.",
        "Deutschkenntnisse sind von Vorteil.",
    ],
)
def test_german_preferred_passes_with_warning(phrase: str) -> None:
    result = apply_hard_filters(to_posting(make_raw(description=f"{phrase} Python.")))
    assert result.outcome is FilterOutcome.PASS
    assert any("German preferred" in w for w in result.warnings)


def test_the_word_germany_alone_is_not_a_language_requirement() -> None:
    assert outcome_for(
        description="Our office is in Germany. English is our working language."
    ) is (FilterOutcome.PASS)


# --- employment type ------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Working Student Data Engineer",
        "Internship Machine Learning",
        "Master Thesis Computer Vision",
        "Werkstudent AI",
    ],
)
def test_non_permanent_titles_are_rejected(title: str) -> None:
    assert outcome_for(title=title) is FilterOutcome.REJECT


def test_declared_freelance_is_rejected() -> None:
    assert outcome_for(employment_type="Freelance") is FilterOutcome.REJECT


# --- seniority ------------------------------------------------------------


@pytest.mark.parametrize("title", ["Head of Data", "VP Engineering", "Director of AI", "CTO"])
def test_executive_titles_are_rejected(title: str) -> None:
    assert outcome_for(title=title) is FilterOutcome.REJECT


def test_junior_titles_are_rejected() -> None:
    assert outcome_for(title="Junior AI Engineer") is FilterOutcome.REJECT


def test_senior_ic_title_passes() -> None:
    assert outcome_for(title="Senior AI Engineer") is FilterOutcome.PASS


# --- eligibility ----------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "Applicants must be EU citizens.",
        "EU/EEA nationals only.",
        "An active security clearance is required.",
        "NATO secret clearance needed.",
    ],
)
def test_unavailable_eligibility_is_rejected(phrase: str) -> None:
    assert outcome_for(description=phrase) is FilterOutcome.REJECT


def test_ambiguous_eligibility_goes_to_manual_review_not_reject() -> None:
    result = apply_hard_filters(
        to_posting(make_raw(description="You must have the right to work in Germany. Python."))
    )
    assert result.outcome is FilterOutcome.MANUAL_REVIEW


# --- location -------------------------------------------------------------


def test_non_german_location_is_rejected() -> None:
    assert outcome_for(location="Paris, France") is FilterOutcome.REJECT


def test_remote_passes() -> None:
    assert outcome_for(location="Remote") is FilterOutcome.PASS


def test_remote_among_other_places_passes() -> None:
    assert outcome_for(location="Remote, New York, Canada, Dubai") is FilterOutcome.PASS


@pytest.mark.parametrize("location", ["Remote US, West Coast", "Remote Canada"])
def test_remote_limited_to_a_region_is_flagged_as_non_german(location: str) -> None:
    result = apply_hard_filters(to_posting(make_raw(location=location)))
    assert any("remote role with non-German location" in w for w in result.warnings)


def test_munich_passes_with_a_warning() -> None:
    result = apply_hard_filters(to_posting(make_raw(location="München")))
    assert result.outcome is FilterOutcome.PASS
    assert any("munich" in w for w in result.warnings)


def test_missing_location_goes_to_manual_review() -> None:
    assert outcome_for(location="") is FilterOutcome.MANUAL_REVIEW


@pytest.mark.parametrize(
    "location",
    [
        "Berlin, Berlin, Germany",
        "Berlin, Berlin, Germany; Berlin, Berlin, Germany",
        "Munich - Berlin",
        "Berlin; London; Munich",
        "Aurora, IL; Irving, TX; Berlin",
    ],
)
def test_one_allowed_place_keeps_the_posting(location: str) -> None:
    """A posting open in Berlin is open in Berlin, whatever else its board lists."""
    assert outcome_for(location=location) is not FilterOutcome.REJECT


def test_the_places_that_are_not_allowed_are_still_named() -> None:
    result = apply_hard_filters(to_posting(make_raw(location="Berlin; London")))
    assert result.outcome is FilterOutcome.PASS
    assert any("london" in warning for warning in result.warnings)


# --- precedence -----------------------------------------------------------


def test_rejection_wins_over_manual_review() -> None:
    result = apply_hard_filters(
        to_posting(
            make_raw(
                location="Paris, France",
                description="You must have the right to work in France.",
            )
        )
    )
    assert result.outcome is FilterOutcome.REJECT


def test_reasons_are_recorded_for_every_rejection() -> None:
    result = apply_hard_filters(to_posting(make_raw(title="Head of AI")))
    assert result.reasons


# --- profile relevance ---------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Staff AI Engineer",
        "Principal Data Engineer",
        "Staff Software Engineer — Context Model Core Team",
        "Machine Learning Engineer (Staff)",
        "Technical Fellow, Machine Learning",
    ],
)
def test_upper_ic_seniority_is_rejected(title: str) -> None:
    assert outcome_for(title=title) is FilterOutcome.REJECT


@pytest.mark.parametrize(
    "title",
    [
        # "staff" as part of a team or org name, not a seniority band.
        "Data Engineer, Staff Platform Team",
        "Python Engineer — Staff Scheduling Product",
        # "principal" describing the product, not the role.
        "Machine Learning Engineer, Principal Component Analysis Tooling",
    ],
)
def test_band_words_outside_the_seniority_slot_do_not_reject(title: str) -> None:
    assert outcome_for(title=title) is not FilterOutcome.REJECT


def test_executive_and_band_rejections_keep_distinct_reasons() -> None:
    assert (
        "executive or people-leadership title"
        in apply_hard_filters(to_posting(make_raw(title="Head of Data Engineering"))).reasons
    )
    assert (
        "seniority above target band"
        in apply_hard_filters(to_posting(make_raw(title="Staff Python Engineer"))).reasons
    )


def test_excluded_role_family_is_rejected() -> None:
    assert outcome_for(title="Frontend Engineer") is FilterOutcome.REJECT


@pytest.mark.parametrize(
    "title",
    [
        # All four reached the pass set of a real run: a description mentioning Python
        # was the only thing keeping them, because nothing tested the title.
        "Credit Risk Manager (Mortgage)",
        "Financial Accountant",
        "IT Support Specialist",
        "Executive Briefing Center Lead (Content & Immersive Experience)",
    ],
)
def test_a_title_outside_the_role_families_never_passes(title: str) -> None:
    assert outcome_for(title=title) is not FilterOutcome.PASS


@pytest.mark.parametrize(
    "title",
    [
        "Senior AI/ML Ops Engineer, AI Platform Team",
        "Site Reliability Engineer - Data Platform",
        "Senior Data Scientist, Growth Data Products",
        "Forward Deployed Engineer (m/w/d)",
    ],
)
def test_a_title_the_gate_recognises_still_passes(title: str) -> None:
    assert outcome_for(title=title) is FilterOutcome.PASS


def test_an_unrecognised_title_is_parked_not_dropped() -> None:
    """A term list is as likely to be short as a posting is to be wrong."""
    result = apply_hard_filters(to_posting(make_raw(title="Workforce Management (Manager)")))
    assert result.outcome is FilterOutcome.MANUAL_REVIEW
    assert "title outside the stated role families" in result.reasons


def test_an_empty_title_list_turns_the_gate_off() -> None:
    config = FilterConfig(title_terms=())
    posting = to_posting(make_raw(title="Workforce Management (Manager)"))
    result = apply_hard_filters(posting, config)
    assert result.outcome is FilterOutcome.PASS


def test_zero_core_stack_hits_is_rejected() -> None:
    result = apply_hard_filters(
        to_posting(
            make_raw(
                title="Software Engineer", description="Permanent role using Java and Spring Boot."
            )
        )
    )
    assert result.outcome is FilterOutcome.REJECT
    assert "no profile-stack signal" in result.reasons


def test_mixed_python_and_typescript_role_passes() -> None:
    assert (
        outcome_for(title="Backend Engineer", description="Python backend and TypeScript UI.")
        is FilterOutcome.PASS
    )


def test_excluded_stack_dominance_parks_but_does_not_reject() -> None:
    result = apply_hard_filters(
        to_posting(
            make_raw(
                title="Backend Engineer",
                description="Permanent role using React, Vue, TypeScript, and one Python script.",
            )
        )
    )
    assert result.outcome is FilterOutcome.MANUAL_REVIEW


def test_repeated_excluded_term_counts_once() -> None:
    assert outcome_for(description="Python and React, React, React.") is FilterOutcome.PASS


def test_empty_core_stack_terms_disables_stack_rules() -> None:
    result = apply_hard_filters(
        to_posting(make_raw(title="Software Engineer", description="Permanent Java role.")),
        FilterConfig(core_stack_terms=()),
    )
    assert result.outcome is FilterOutcome.PASS


def test_location_reason_precedes_stack_reason() -> None:
    result = apply_hard_filters(
        to_posting(make_raw(location="Paris, France", description="Permanent Java role."))
    )
    assert result.reasons[0].startswith("location outside Germany")


# --- term matching on lowered text ---------------------------------------
# `_term_hits` skips a term whose first word is absent before running its pattern. The
# probe is an optimisation and must never decide anything on its own.


@pytest.mark.parametrize(
    ("text", "terms", "expected"),
    [
        ("Built with PyTorch daily.", ("pytorch",), {"pytorch"}),
        ("we use PYTORCH here", ("pytorch",), {"pytorch"}),
        # Word boundaries survive: a term must not match inside a longer word.
        ("Strong javascript background.", ("java",), set()),
        ("Runs on java 21.", ("java",), {"java"}),
        ("Pythonic code.", ("python",), set()),
        # Multi-word terms tolerate any run of whitespace, including a newline.
        ("machine   learning models", ("machine learning",), {"machine learning"}),
        ("machine\nlearning models", ("machine learning",), {"machine learning"}),
        # The probe is the first word, so a first word present without the rest is not a hit.
        ("machine tooling", ("machine learning",), set()),
        # Punctuation-bearing terms are escaped, not treated as pattern syntax.
        ("Mostly C# and .NET.", ("c#", ".net"), {"c#", ".net"}),
        ("Team uses Hugging Face transformers.", ("hugging face",), {"hugging face"}),
        # An empty term is skipped rather than matching everywhere.
        ("anything at all", ("", "kafka"), set()),
    ],
)
def test_term_hits_on_lowered_text(text: str, terms: tuple[str, ...], expected: set[str]) -> None:
    assert _term_hits(text.lower(), terms) == expected


def test_term_hits_requires_its_text_already_lowered() -> None:
    """The contract is the caller's job; stating it as a test stops it being forgotten."""
    assert _term_hits("PYTORCH", ("pytorch",)) == set()
    assert _term_hits("pytorch", ("pytorch",)) == {"pytorch"}


def test_a_configured_term_in_mixed_case_still_matches() -> None:
    """Preferences store lowercase terms, but nothing enforces it at the boundary."""
    assert _term_hits("we run kubernetes", ("Kubernetes",)) == {"Kubernetes"}


def test_german_gate_does_not_swallow_a_real_requirement() -> None:
    """The substring gate in front of the German patterns skips postings that name no
    German at all. A posting that does name it must still reach the level comparison."""
    assert outcome_for(description="Fluent German required.") is FilterOutcome.REJECT
    assert (
        apply_hard_filters(to_posting(make_raw(description="German is a plus. Python."))).warnings
    ) == ("German preferred but not required",)


def test_german_wording_in_upper_case_is_still_a_requirement() -> None:
    assert outcome_for(description="FLUENT GERMAN REQUIRED.") is FilterOutcome.REJECT


def test_deutsch_wording_reaches_the_language_rule() -> None:
    """The gate probes both `german` and `deutsch`; German-language wording uses neither
    interchangeably."""
    assert outcome_for(description="Verhandlungssichere Deutschkenntnisse.") is (
        FilterOutcome.REJECT
    )


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("\u0130OS", "ios"),
        ("sc\u0131kit-learn", "scikit-learn"),
        ("\u017fpring boot", "spring boot"),
        ("\u212aafka", "kafka"),
        ("\u039f\u03a3", "\u03bf\u03c3"),
        ("Straße", "strasse"),
        ("anything at all", "   "),
        ("anything at all", "\t\n"),
    ],
)
def test_unicode_and_blank_terms_preserve_regex_matching(text: str, term: str) -> None:
    expected = {term} if term_pattern(term).search(text) else set()
    assert _term_hits(_match_text(text), (term,)) == expected


@pytest.mark.parametrize("description", ["German requ\u0130red.", "German requ\u0131red."])
def test_unicode_german_requirement_is_rejected(description: str) -> None:
    assert outcome_for(description=f"Python. {description}") is FilterOutcome.REJECT


def test_unicode_excluded_role_is_rejected() -> None:
    assert outcome_for(title="\u0130OS Developer", description="Python permanent role.") is (
        FilterOutcome.REJECT
    )
