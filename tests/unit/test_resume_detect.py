"""Detection is deterministic, vocabulary-driven, and free of substring hits."""

from __future__ import annotations

import pytest

from career_copilot.domain.resume_detect import detect

TERMS = ("python", "llm", "rag", "kubernetes", "fastapi", "java", "computer vision")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Built services in Python.", ("Python",)),
        ("Shipped an LLM feature with RAG.", ("LLM / RAG",)),
        ("Wrote JavaScript all day.", ()),
        ("Ran Kubernetes and pure Java services.", ("Kubernetes", "Java")),
        ("Computer  vision research.", ("Computer Vision",)),
    ],
)
def test_skill_detection(text: str, expected: tuple[str, ...]) -> None:
    assert detect(text, TERMS).skills == expected


def test_skill_order_follows_the_vocabulary_not_the_text() -> None:
    assert detect("kubernetes then python", TERMS).skills == ("Python", "Kubernetes")


def test_roles_come_from_family_terms() -> None:
    roles = detect("PyTorch models deployed on Kubernetes with Airflow.", TERMS).roles
    assert roles == ("AI/ML Engineering", "Platform/Infra", "Data Engineering")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Languages: German (B2), English C1", [("English", "C1"), ("German", "B2")]),
        ("Languages: English, German", [("English", None), ("German", None)]),
        ("Kannada - native speaker", [("Kannada", "native")]),
        # A level stated before the language binds just as well as one after it.
        ("C1 English and B2 German", [("English", "C1"), ("German", "B2")]),
        ("Fluent in English (C2) and German (B1)", [("English", "C2"), ("German", "B1")]),
        ("German: native, English: C1", [("English", "C1"), ("German", "native")]),
        # A level belongs to one language: it never leaks onto its neighbour.
        ("Languages: English, German B2", [("English", None), ("German", "B2")]),
        ("Languages: English C1, German", [("English", "C1"), ("German", None)]),
        ("English\nGerman B2", [("English", None), ("German", "B2")]),
        # Too far away to be this language's level.
        ("English is used across every team we worked with, C1", [("English", None)]),
    ],
)
def test_language_levels(text: str, expected: list[tuple[str, str | None]]) -> None:
    found = detect(text, TERMS).languages
    assert [(item.name, item.level) for item in found] == expected


def test_empty_vocabulary_detects_no_skills() -> None:
    assert detect("Python and Kubernetes", ()).skills == ()
