"""Deterministic resume detection. Pure: text in, detections out, no I/O."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from career_copilot.domain.filters import term_pattern

# Role families. A family is detected when any of its terms appears. Declaration
# order is the output order, so the same resume always yields the same chips.
# The family name is the display label; its match term is that name lowercased.
ROLE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "AI/ML Engineering",
        (
            "machine learning",
            "ml engineer",
            "deep learning",
            "llm",
            "genai",
            "generative ai",
            "pytorch",
            "tensorflow",
            "nlp",
            "rag",
            "agentic",
        ),
    ),
    (
        "Platform/Infra",
        (
            "kubernetes",
            "terraform",
            "docker",
            "devops",
            "sre",
            "platform engineer",
            "infrastructure",
            "ci/cd",
            "ci-cd",
        ),
    ),
    ("Backend", ("backend", "back-end", "fastapi", "django", "flask", "microservice", "rest api")),
    (
        "Data Engineering",
        ("data engineer", "etl", "airflow", "databricks", "kafka", "spark", "data pipeline"),
    ),
    ("Research", ("research", "thesis", "publication", "master's thesis")),
)

# Language names looked for near a level token.
LANGUAGE_NAMES: tuple[str, ...] = (
    "English",
    "German",
    "French",
    "Spanish",
    "Italian",
    "Dutch",
    "Portuguese",
    "Hindi",
    "Kannada",
    "Marathi",
    "Russian",
    "Mandarin",
    "Chinese",
    "Japanese",
    "Arabic",
    "Polish",
    "Turkish",
)

_CANONICAL_LANGUAGE = {name.lower(): name for name in LANGUAGE_NAMES}

# Casing the chips are shown in. Anything not listed falls back to title case.
SKILL_LABELS: dict[str, str] = {
    "llm": "LLM / RAG",
    "rag": "LLM / RAG",
    "genai": "GenAI",
    "generative ai": "GenAI",
    "fastapi": "FastAPI",
    "pytorch": "PyTorch",
    "mlops": "MLOps",
    "ci/cd": "CI/CD",
    "sql": "SQL",
    "aws": "AWS",
    "gcp": "GCP",
    "ros2": "ROS2",
    "postgresql": "PostgreSQL",
}

# A level binds to a language only when the two are adjacent tokens with at most
# this much punctuation between them. Wider windows let "English, German B2" hand
# English a level it never claimed.
_LEVEL_GAP = 12
_LEVEL = re.compile(r"\b([ABC][12])\b|\b(native|mother\s+tongue|muttersprache)\b", re.IGNORECASE)
_LANGUAGE = re.compile(r"(?<!\w)(?:" + "|".join(LANGUAGE_NAMES) + r")(?!\w)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DetectedLanguage:
    """A language named in the resume, with its level when one was stated."""

    name: str
    level: str | None


@dataclass(frozen=True, slots=True)
class ResumeDetections:
    """Everything derived from a resume. The text itself is never kept.

    `roles` and `skills` are display labels; `skill_terms` are the match terms that
    produced them. Both are kept because a label and a term are different things: `LLM /
    RAG` is a chip, `llm` and `rag` are what match a posting. Preferences prefill reads
    the terms, so a prefilled skill still matches exactly what it matched during
    detection — an inverted label map could not say which of `llm` or `rag` had hit.
    """

    roles: tuple[str, ...]
    skills: tuple[str, ...]
    languages: tuple[DetectedLanguage, ...]
    skill_terms: tuple[str, ...] = ()

    @property
    def role_terms(self) -> tuple[str, ...]:
        return tuple(role.lower() for role in self.roles)


def label_for(term: str) -> str:
    """Display casing for a match term. Presentation only — never stored, never matched."""
    return SKILL_LABELS.get(term.lower(), term.title())


def _skills(text: str, skill_terms: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The labels to show, and the terms that produced them."""
    hits = [term for term in skill_terms if term and term_pattern(term).search(text)]
    return tuple(dict.fromkeys(label_for(term) for term in hits)), tuple(dict.fromkeys(hits))


def _roles(text: str) -> tuple[str, ...]:
    return tuple(
        family
        for family, terms in ROLE_FAMILIES
        if any(term_pattern(term).search(text) for term in terms)
    )


@dataclass(frozen=True, slots=True)
class _Token:
    """One language name or one level word, with where it sits in the text."""

    start: int
    end: int
    language: str | None
    level: str | None


def _tokens(text: str) -> list[_Token]:
    found = [
        _Token(m.start(), m.end(), _CANONICAL_LANGUAGE[m.group(0).lower()], None)
        for m in _LANGUAGE.finditer(text)
    ]
    found += [
        _Token(m.start(), m.end(), None, m.group(1).upper() if m.group(1) else "native")
        for m in _LEVEL.finditer(text)
    ]
    found.sort(key=lambda token: token.start)
    return found


def _gap(text: str, left: _Token, right: _Token) -> int | None:
    """Characters between the two tokens, or None if they are not adjacent."""
    gap = text[left.end : right.start]
    return None if len(gap) > _LEVEL_GAP or "\n" in gap else len(gap)


def _levels_by_language(text: str) -> dict[str, str]:
    """Bind each level word to the one language name it sits beside.

    A language takes the nearer of the level words on either side of it, so both
    "English C1" and "C1 English" bind. A claimed level is never reused, so in
    "English, German B2" the B2 stays with German and English keeps no level.
    """
    tokens = _tokens(text)
    levels: dict[str, str] = {}
    claimed: set[int] = set()
    for index, token in enumerate(tokens):
        if token.language is None or token.language in levels:
            continue
        candidates: list[tuple[int, int, str]] = []
        for neighbour in (index - 1, index + 1):
            if not 0 <= neighbour < len(tokens) or neighbour in claimed:
                continue
            other = tokens[neighbour]
            if other.level is None:
                continue
            left, right = (other, token) if neighbour < index else (token, other)
            gap = _gap(text, left, right)
            if gap is not None:
                candidates.append((gap, neighbour, other.level))
        if candidates:
            _, neighbour, level = min(candidates)
            levels[token.language] = level
            claimed.add(neighbour)
    return levels


def _languages(text: str) -> tuple[DetectedLanguage, ...]:
    levels = _levels_by_language(text)
    named = {token.language for token in _tokens(text) if token.language is not None}
    return tuple(
        DetectedLanguage(name=name, level=levels.get(name))
        for name in LANGUAGE_NAMES
        if name in named
    )


def detect(text: str, skill_terms: Sequence[str]) -> ResumeDetections:
    """Detect roles, skills, and languages in resume text.

    `skill_terms` is the same curated vocabulary the hard filters use, so
    detection and filtering cannot drift apart.
    """
    skills, terms = _skills(text, skill_terms)
    return ResumeDetections(
        roles=_roles(text), skills=skills, languages=_languages(text), skill_terms=terms
    )
