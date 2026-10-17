"""Extraction returns text, or says clearly that it could not."""

from __future__ import annotations

import pytest

from career_copilot.adapters.resume.extract import ResumeExtractionError, extract
from tests.resume_fixtures import make_docx, make_encrypted_pdf, make_pdf


def test_pdf_text() -> None:
    assert "Python" in extract("cv.pdf", make_pdf("Python and Kubernetes"))


def test_docx_text() -> None:
    text = extract("cv.docx", make_docx("Anna Schmidt", "Python, FastAPI"))
    assert "Anna Schmidt" in text
    assert "FastAPI" in text


def test_empty_pdf_is_rejected() -> None:
    with pytest.raises(ResumeExtractionError):
        extract("cv.pdf", make_pdf(""))


def test_encrypted_pdf_is_rejected() -> None:
    with pytest.raises(ResumeExtractionError):
        extract("cv.pdf", make_encrypted_pdf("Python"))


def test_docx_without_text_is_rejected() -> None:
    with pytest.raises(ResumeExtractionError):
        extract("cv.docx", make_docx())
