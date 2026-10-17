"""Deterministic hard filters. Run before any model call.

Rules come from profile/preferences.md. A rejection here can never be
outweighed later by a high match score. Ambiguous wording is parked for
manual review rather than dropped silently.

Fixed patterns match text normalized once for case-insensitive matching, rather than with
`re.IGNORECASE` against the original. Saving preferences re-filters the whole stored
table, so this path runs a thousand times per save and its cost is felt by a person
waiting on a form. Normalization preserves the Unicode variants of ASCII letters;
`tests/golden/test_filter_corpus.py` holds that claim to real board wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from career_copilot.domain.models import FilterOutcome, HardFilterResult, JobPosting
from career_copilot.domain.normalize import location_segments

# --- German language ------------------------------------------------------
# Reject only demands above the level the user actually holds. "German is a plus" is a
# warning. Only German is matched: every other language would need its own native-wording
# patterns to work at all, and inventing them for a case that does not exist would be a
# rewrite pretending to be a generalisation.
GERMAN = "german"

# CEFR as a ladder, so "does the demand exceed what I have" is one comparison.
_CEFR_RANK: dict[str, int] = {
    "A1": 1,
    "A2": 2,
    "B1": 3,
    "B2": 4,
    "C1": 5,
    "C2": 6,
    "native": 7,
}

# What a demand is asking for, when it names no level. Fluent, business-fluent,
# verhandlungssicher and native wording all describe C1 or above; a bare "German is
# required" is the weaker claim, and B2 is the level that wording is normally used for.
_WORDING_LEVEL = "C1"
_BARE_REQUIREMENT_LEVEL = "B2"
_NATIVE = re.compile(r"\bnative\b")
_STATED_LEVEL = re.compile(r"\b([bc][12])\b")
_BARE_REQUIREMENT = re.compile(r"\b(?:required|mandatory|a\s+must|essential)\b")
_GERMAN_REQUIRED = re.compile(
    r"(?:fluent|business[- ]fluent|professional|native|verhandlungssicher|fliessend|fließend"
    r"|sehr\s+gute?|excellent|advanced|working\s+proficiency\s+in)\s+(?:\w+\s+){0,3}?german"
    r"|german\s+(?:language\s+)?(?:skills?\s+)?(?:at\s+)?(?:level\s+)?[bc][12]"
    r"|german\s+(?:is\s+)?(?:required|mandatory|a\s+must|essential)"
    r"|deutsch\s*(?:kenntnisse)?\s*(?:auf\s*)?[bc][12]"
    r"|verhandlungssichere?\s+deutschkenntnisse"
    r"|fluency\s+in\s+german",
)
_GERMAN_PREFERRED = re.compile(
    r"\bgerman\b[^.]{0,50}?"
    r"\b(?:plus|bonus|nice[- ]to[- ]have|preferred|advantage|helpful|welcome)\b"
    r"|\b(?:plus|bonus|advantage|preferred)\b[^.]{0,50}?\bgerman\b"
    r"|deutschkenntnisse\s+(?:sind\s+)?(?:von\s+vorteil|wünschenswert)",
)

# --- Employment type ------------------------------------------------------
_NON_PERMANENT = re.compile(
    r"\b(intern(ship)?|praktikum|praktikant|working\s+student|werkstudent|thesis|abschlussarbeit"
    r"|masterarbeit|bachelorarbeit|apprentice|ausbildung|dual\s+study|duales\s+studium"
    r"|freelance|freiberuflich|contractor|contract\s+role|fixed[- ]term|befristet|temporary"
    r"|part[- ]time\s+only|volunteer)\b",
)
_PERMANENT_HINT = re.compile(r"\b(permanent|unbefristet|full[- ]time)\b")

# --- Seniority ------------------------------------------------------------
_TOO_SENIOR = re.compile(
    r"\b(head\s+of|director|vp|vice\s+president|chief|c[te]o|cpo|svp|evp|partner"
    r"|geschäftsführer|managing\s+director)\b",
)
# Staff/Principal band. Positional, not a bare word match: "Staff Engineer" is a
# band, "Data Engineer, Staff Platform Team" is a team name.
_SENIOR_BAND = re.compile(
    r"^(?:staff|principal|distinguished)\b"
    r"|\b(?:staff|principal|distinguished)\s+(?:[\w/&+.-]+\s+){0,2}"
    r"(?:engineer|scientist|developer|architect|researcher|analyst|programmer)\b"
    r"|\b(?:technical|engineering|distinguished|research)\s+fellow\b"
    r"|[,(\[-]\s*(?:staff|principal|distinguished)\s*[)\]]?$",
)
_TOO_JUNIOR = re.compile(r"\b(junior|entry[- ]level|graduate|working\s+student|trainee)\b")
_SENIOR_TITLE = re.compile(r"\b(?:senior|sr\.?)\b")

# --- Eligibility ----------------------------------------------------------
_ELIGIBILITY_BLOCKER = re.compile(
    r"\b(eu\s*/?\s*(?:eea\s+)?(?:citizens?|citizenship|nationals?|passport)|european\s+citizenship"
    r"|nato\s+(?:secret|clearance)|security\s+clearance|sicherheitsüberprüfung"
    r"|government\s+clearance|must\s+hold\s+.{0,20}clearance)\b",
)
_ELIGIBILITY_AMBIGUOUS = re.compile(
    r"\b(work\s+(?:authorization|permit)|eligible\s+to\s+work|right\s+to\s+work"
    r"|background\s+check|arbeitserlaubnis)\b",
)

# --- Relocation -----------------------------------------------------------
_RELOCATION_REQUIRED = re.compile(
    r"\b(relocation\s+(?:to|is)\s+(?!germany|berlin|munich)|must\s+relocate"
    r"|onsite\s+in\s+(?!germany|berlin|munich|münchen)\w+)\b",
)


@dataclass(frozen=True, slots=True)
class FilterConfig:
    """Tunable inputs to the hard filters."""

    allowed_countries: frozenset[str] = frozenset({"germany", "deutschland", "de"})
    allowed_cities: frozenset[str] = frozenset(
        {"berlin", "munich", "hamburg", "cologne", "frankfurt", "remote", ""}
    )
    preferred_cities: frozenset[str] = frozenset({"berlin"})
    flagged_cities: frozenset[str] = frozenset({"munich"})
    allow_remote: bool = True
    require_permanent: bool = True
    excluded_role_terms: tuple[str, ...] = (
        "frontend",
        "front-end",
        "android",
        "ios",
        "mobile developer",
        "qa engineer",
        "sdet",
        "test engineer",
        "salesforce",
        "abap",
        "value engineer",
        "product manager",
        "support specialist",
        "accountant",
    )
    # The positive half of the role gate, matched against the title only. Empty disables
    # it, the same fail-open rule `core_stack_terms` has. Without it the only thing with
    # keep-power was a stack term anywhere in the description, which is why a Credit Risk
    # Manager advert that mentions Python read as a match.
    title_terms: tuple[str, ...] = (
        "data engineer",
        "software engineer",
        "backend engineer",
        "python engineer",
        "platform engineer",
        "ml engineer",
        "machine learning engineer",
        "ai engineer",
        "mlops",
        "ml ops",
        "site reliability engineer",
        "sre",
        "devops engineer",
        "data scientist",
        "applied scientist",
        "research engineer",
        "analytics engineer",
        "infrastructure engineer",
        "cloud engineer",
        "solutions engineer",
        "forward deployed engineer",
        "ai enablement",
        "data platform",
        "ai platform",
        "machine learning",
        "llm",
        "genai",
    )
    core_stack_terms: tuple[str, ...] = (
        "python",
        "llm",
        "genai",
        "generative ai",
        "agentic",
        "rag",
        "machine learning",
        "pytorch",
        "hugging face",
        "kafka",
        "databricks",
        "kubernetes",
        "azure",
        "airflow",
        "mlops",
        "data pipeline",
        "computer vision",
        "fastapi",
    )
    excluded_stack_terms: tuple[str, ...] = (
        "angular",
        "react",
        "vue",
        "spring boot",
        "java",
        ".net",
        "c#",
        "php",
        "ruby on rails",
        "typescript",
    )
    max_per_company: int = 2
    # Which bands the user is open to. Each seniority rule is conditional on its band
    # being absent, so the default reproduces the previous unconditional behaviour:
    # executive, staff/principal, and junior titles are all rejected.
    seniority_bands: tuple[str, ...] = ("mid", "senior")
    # (language key, CEFR level) for what the user *has*. Only German is consulted.
    language_levels: tuple[tuple[str, str], ...] = (("english", "C2"), ("german", "B1"))


@dataclass
class _Verdict:
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)

    def result(self) -> HardFilterResult:
        if self.reasons:
            outcome = FilterOutcome.REJECT
        elif self.review:
            outcome = FilterOutcome.MANUAL_REVIEW
        else:
            outcome = FilterOutcome.PASS
        return HardFilterResult(
            outcome=outcome,
            reasons=tuple(self.reasons + self.review),
            warnings=tuple(self.warnings),
        )


def _demanded_level(demand: str) -> str:
    """The CEFR level a matched German demand is asking for."""
    if _NATIVE.search(demand):
        return "native"
    stated = _STATED_LEVEL.search(demand)
    if stated:
        return stated.group(1).upper()
    if _BARE_REQUIREMENT.search(demand):
        return _BARE_REQUIREMENT_LEVEL
    return _WORDING_LEVEL


def _check_language(low: str, cfg: FilterConfig, v: _Verdict) -> None:
    # Both German patterns are alternations with bounded `[^.]{0,50}` spans, and roughly
    # half of all postings name no German at all. One substring test skips them.
    if "german" not in low and "deutsch" not in low:
        return
    demands = list(_GERMAN_REQUIRED.finditer(low))
    if not demands:
        if _GERMAN_PREFERRED.search(low):
            v.warnings.append("German preferred but not required")
        return
    demanded = max(
        (_demanded_level(demand.group(0)) for demand in demands), key=_CEFR_RANK.__getitem__
    )
    held = dict(cfg.language_levels).get(GERMAN)
    if held is None:
        # Never a silent pass: the posting demands a language the preferences do not
        # mention, which is a question for the user rather than a decision for a rule.
        v.review.append(f"requires German at {demanded}, no German level stated")
    elif _CEFR_RANK.get(held, 0) < _CEFR_RANK.get(demanded, 0):
        v.reasons.append(f"requires German at {demanded} (profile is {held})")
    else:
        v.warnings.append(f"requires German at {demanded} (profile is {held})")


def _check_employment(
    posting: JobPosting, low: str, low_title: str, cfg: FilterConfig, v: _Verdict
) -> None:
    if not cfg.require_permanent:
        return
    declared = _match_text(posting.employment_type or "")
    if declared and _NON_PERMANENT.search(declared):
        v.reasons.append(f"not permanent employment: {posting.employment_type}")
        return
    if _NON_PERMANENT.search(low_title):
        v.reasons.append("title indicates non-permanent role")
        return
    if not declared and not _PERMANENT_HINT.search(low) and _NON_PERMANENT.search(low):
        v.review.append("employment type unclear, non-permanent wording in description")


def _check_seniority(low_title: str, cfg: FilterConfig, v: _Verdict) -> None:
    """Each rule fires only while its band is unselected, so widening the bands widens
    the search rather than needing the regexes rewritten."""
    bands = set(cfg.seniority_bands)
    if _TOO_SENIOR.search(low_title):
        if "lead" not in bands:
            v.reasons.append("executive or people-leadership title")
    elif _SENIOR_BAND.search(low_title):
        if "staff" not in bands:
            v.reasons.append("seniority above target band")
    elif _TOO_JUNIOR.search(low_title):
        if "junior" not in bands:
            v.reasons.append("below mid-level")
    elif _SENIOR_TITLE.search(low_title):
        if "senior" not in bands:
            v.reasons.append("senior band not selected")
    elif "mid" not in bands:
        v.reasons.append("mid-level or unspecified band not selected")


def _match_text(text: str) -> str:
    """Lowercase without losing Python regex's Unicode equivalents of i and s.

    Replace dotted I before lower() expands it into two code points. Kelvin sign
    already lowers to k. Avoid casefold(), which would also expand words like Straße.
    """
    return text.replace("\u0130", "i").lower().replace("\u0131", "i").replace("\u017f", "s")


def _phrase(term: str) -> str:
    return r"\s+".join(re.escape(word) for word in term.split())


@lru_cache(maxsize=512)
def term_pattern(term: str) -> re.Pattern[str]:
    """Match a configured word or phrase, with flexible phrase whitespace.

    Case-insensitive, for callers holding text in its original case
    (`domain/resume_detect.py`, `adapters/pii/person_policy.py`).
    """
    return re.compile(rf"(?<!\w){_phrase(term)}(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=512)
def _lower_term_pattern(term: str) -> tuple[str, re.Pattern[str]]:
    """A probe and a pattern for matching against already-lowercased text.

    The probe is the term's first word. A term whose first word does not occur cannot
    match, and `str.__contains__` settles that far faster than the regex engine does.
    """
    # Arbitrary Unicode preferences retain the regex engine's case equivalences
    # (for example, Greek sigma). An empty probe bypasses the substring shortcut.
    if not term.isascii():
        return "", term_pattern(term)
    lowered = term.lower()
    words = lowered.split()
    return words[0] if words else "", re.compile(rf"(?<!\w){_phrase(lowered)}(?!\w)")


def _term_hits(low: str, terms: tuple[str, ...]) -> set[str]:
    """Which of `terms` occur in `low`, which must already be normalized by `_match_text`."""
    hits: set[str] = set()
    for term in terms:
        if not term:
            continue
        probe, pattern = _lower_term_pattern(term)
        if probe in low and pattern.search(low):
            hits.add(term)
    return hits


def matching_terms(text: str, terms: tuple[str, ...]) -> set[str]:
    """Which of `terms` the rules would find in `text`, matched exactly as the filters do."""
    return _term_hits(_match_text(text), terms)


def _check_role_family(low_title: str, cfg: FilterConfig, v: _Verdict) -> None:
    """Both halves of the gate: an excluded title is out, an unrecognised title is parked.

    Parked, never dropped: a title the term list does not know is a gap in the list as
    often as it is a bad posting, and the review screen shows manual_review behind one
    toggle. Only `excluded_role_terms` rejects.
    """
    hits = _term_hits(low_title, cfg.excluded_role_terms)
    if hits:
        v.reasons.append(f"excluded role family: {', '.join(sorted(hits))}")
        return
    if cfg.title_terms and not _term_hits(low_title, cfg.title_terms):
        v.review.append("title outside the stated role families")


def _check_stack(low: str, cfg: FilterConfig, v: _Verdict) -> None:
    # An empty core inventory explicitly disables both stack rules (fail open).
    if not cfg.core_stack_terms:
        return
    core = _term_hits(low, cfg.core_stack_terms)
    excluded = _term_hits(low, cfg.excluded_stack_terms)
    if not core:
        v.reasons.append("no profile-stack signal")
    elif len(excluded) > len(core):
        v.review.append(f"excluded stack dominates profile stack ({len(excluded)} > {len(core)})")


def _check_eligibility(low: str, v: _Verdict) -> None:
    if _ELIGIBILITY_BLOCKER.search(low):
        v.reasons.append("citizenship or security-clearance requirement")
    elif _ELIGIBILITY_AMBIGUOUS.search(low):
        v.review.append("eligibility wording ambiguous")


def _check_location(posting: JobPosting, low: str, cfg: FilterConfig, v: _Verdict) -> None:
    """One allowed place is enough. A posting open in Berlin and London is open in Berlin.

    Read from `posting.location`, not `location_norm`: the stored token is the primary
    segment only, and a rule that saw just that would drop every posting whose board lists
    Berlin second.
    """
    segments = location_segments(posting.location)
    if not segments:
        v.review.append("location missing")
        return
    for segment in segments:
        if segment in cfg.flagged_cities:
            v.warnings.append(f"{segment}: confirm Berlin arrangement")
    allowed = tuple(
        segment
        for segment in segments
        if segment in cfg.allowed_cities and (segment != "remote" or cfg.allow_remote)
    )
    if allowed:
        others = tuple(segment for segment in segments if segment not in allowed)
        if others:
            v.warnings.append(f"also listed: {', '.join(others)}")
        return
    if segments == ("remote",):
        v.reasons.append("remote not allowed by config")
        return
    if posting.remote and cfg.allow_remote:
        v.warnings.append(f"remote role with non-German location: {posting.location}")
        return
    if _RELOCATION_REQUIRED.search(low):
        v.reasons.append(f"relocation required to {posting.location}")
        return
    v.reasons.append(f"location outside Germany: {posting.location}")


def apply_hard_filters(posting: JobPosting, cfg: FilterConfig | None = None) -> HardFilterResult:
    """Rule-based keep/park/drop decision. No model call, no network."""
    cfg = cfg or FilterConfig()
    # Normalize once for the fixed patterns and ASCII preference terms.
    low = _match_text(f"{posting.title}\n{posting.description}")
    low_title = _match_text(posting.title)
    v = _Verdict()
    _check_location(posting, low, cfg, v)
    _check_language(low, cfg, v)
    _check_employment(posting, low, low_title, cfg, v)
    _check_seniority(low_title, cfg, v)
    _check_role_family(low_title, cfg, v)
    _check_stack(low, cfg, v)
    _check_eligibility(low, v)
    return v.result()
