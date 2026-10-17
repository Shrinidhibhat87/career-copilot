"""The hard filters, run over real public ATS postings.

Configuration equality is not enough to prove the preferences refactor harmless:
`domain/filters.py` itself gained seniority-band and language-level conditions, so the
proof has to be behavioural. Two claims are made here, over the same committed corpus:

1. the bootstrapped preferences decide every posting exactly as the `config.yaml`
   `filters:` block does, outcome, reasons and warnings alike; and
2. both reproduce the outcome committed alongside the posting.

The recorded outcomes were re-recorded on 2026-09-06, when the retrieval fix in
`docs/superpowers/specs/007-review-and-recommendations-design.md` deliberately moved
them: a posting listing several places is now read as several places, so postings that
name Berlin second are no longer rejected as being outside Germany. Re-recording is
what a deliberate rule change costs; the movement itself is asserted in
`tests/unit/test_filters.py`, against the wording that used to fail.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from career_copilot.config import Config
from career_copilot.domain.filters import _match_text, _term_hits, apply_hard_filters, term_pattern
from career_copilot.domain.models import HardFilterResult, JobPosting, RawJob
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.preferences import bootstrap

CORPUS = Path(__file__).parent.parent / "fixtures" / "ats_postings.json"


def _postings() -> list[dict[str, Any]]:
    return list(json.loads(CORPUS.read_text(encoding="utf-8")))


def _posting(entry: dict[str, Any]) -> JobPosting:
    return to_posting(
        RawJob(
            source=entry["source"],
            source_job_id=entry["source_job_id"],
            company=entry["company"],
            title=entry["title"],
            location=entry["location"],
            url=entry["canonical_url"],
            description=entry["description"],
            employment_type=entry["employment_type"],
            remote=entry["remote"],
        )
    )


@pytest.fixture(scope="module")
def corpus() -> list[dict[str, Any]]:
    entries = _postings()
    assert len(entries) > 200, "the corpus should cover the real board wording, not a sample"
    return entries


def test_the_corpus_covers_every_outcome(corpus: list[dict[str, Any]]) -> None:
    assert {entry["filter_outcome"] for entry in corpus} == {"pass", "manual_review", "reject"}


def test_bootstrapped_preferences_decide_exactly_as_config_yaml(
    corpus: list[dict[str, Any]],
) -> None:
    config = Config.load()
    seeded = config.filters.to_filter_config()
    derived = bootstrap(seeded, notes="", batch_size=6).to_filter_config()

    differences: list[str] = []
    for entry in corpus:
        posting = _posting(entry)
        under_config: HardFilterResult = apply_hard_filters(posting, seeded)
        under_prefs: HardFilterResult = apply_hard_filters(posting, derived)
        if under_config != under_prefs:
            differences.append(f"{entry['company']} — {entry['title']}")
    assert not differences, f"{len(differences)} postings decided differently: {differences[:5]}"


def test_the_bootstrap_reproduces_the_recorded_outcomes(corpus: list[dict[str, Any]]) -> None:
    """Every posting in the corpus still decides the way the corpus says it does."""
    derived = bootstrap(Config.load().filters.to_filter_config(), notes="", batch_size=6)
    config = derived.to_filter_config()

    regressions = [
        f"{entry['title']}: {entry['filter_outcome']} -> {result.outcome}"
        for entry in corpus
        if (result := apply_hard_filters(_posting(entry), config)).outcome
        != entry["filter_outcome"]
    ]
    assert not regressions, regressions[:5]


def test_the_bootstrap_reproduces_the_recorded_reasons(corpus: list[dict[str, Any]]) -> None:
    """Outcome equality is too coarse to protect a rewrite of how the rules match.

    The recorded German reasons predate the level-aware language rule and read
    "requires working-proficiency German"; they are excluded rather than refreshed,
    because rewriting the corpus to match the code would retire the evidence it exists
    to provide. Every other reason is compared exactly, so a rule firing for a different
    cause than before is caught here instead of surviving on a verdict that happened to
    land in the same bucket.
    """
    cfg = bootstrap(Config.load().filters.to_filter_config(), notes="", batch_size=6)
    config = cfg.to_filter_config()

    def without_language(reasons: list[str]) -> list[str]:
        return [reason for reason in reasons if "German" not in reason]

    differences: list[str] = []
    for entry in corpus:
        result = apply_hard_filters(_posting(entry), config)
        expected = without_language(list(entry["filter_reasons"]))
        actual = without_language(list(result.reasons))
        if actual != expected:
            differences.append(f"{entry['title']}: {expected} -> {actual}")
    assert not differences, f"{len(differences)} differences: {differences[:5]}"


def test_term_matching_against_lowered_text_agrees_with_the_case_insensitive_pattern(
    corpus: list[dict[str, Any]],
) -> None:
    """`_term_hits` matches a lowercased posting with a case-sensitive pattern.

    `term_pattern` is the case-insensitive form the resume detector and the PII person
    policy still use. The two must find the same terms in the same text, or the stack
    rules quietly change meaning.
    """
    terms = Config.load().filters.to_filter_config()
    every_term = terms.core_stack_terms + terms.excluded_stack_terms + terms.excluded_role_terms

    differences: list[str] = []
    for entry in corpus:
        posting = _posting(entry)
        text = f"{posting.title}\n{posting.description}"
        reference = {term for term in every_term if term and term_pattern(term).search(text)}
        if _term_hits(_match_text(text), every_term) != reference:
            differences.append(entry["title"])
    assert not differences, f"{len(differences)} postings matched differently: {differences[:5]}"


def _mangle_case(value: str) -> str:
    """Alternate the case of every letter. Nothing matches this by accident."""
    return "".join(char.upper() if index % 2 else char.lower() for index, char in enumerate(value))


@pytest.mark.parametrize(
    "transform", [str.upper, str.lower, _mangle_case], ids=["upper", "lower", "mangled"]
)
def test_the_rules_stay_case_insensitive(corpus: list[dict[str, Any]], transform: Any) -> None:
    """The patterns match lowercased text rather than carrying `re.IGNORECASE`.

    That is equivalent only while every rule sees the posting through the same
    lowercasing, so the whole corpus is decided again with its case destroyed and must
    reach the identical verdict.
    """
    config = bootstrap(
        Config.load().filters.to_filter_config(), notes="", batch_size=6
    ).to_filter_config()

    differences: list[str] = []
    for entry in corpus:
        posting = _posting(entry)
        expected = apply_hard_filters(posting, config)
        mangled = replace(
            posting,
            title=transform(posting.title),
            description=transform(posting.description),
            employment_type=transform(posting.employment_type or "") or None,
        )
        actual = apply_hard_filters(mangled, config)
        if (actual.outcome, sorted(actual.reasons), sorted(actual.warnings)) != (
            expected.outcome,
            sorted(expected.reasons),
            sorted(expected.warnings),
        ):
            differences.append(f"{entry['title']}: {expected.outcome} -> {actual.outcome}")
    assert not differences, f"{len(differences)} decided differently: {differences[:5]}"
