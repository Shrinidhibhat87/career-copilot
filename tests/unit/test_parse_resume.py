"""The upload use case's privacy preview: full redacted text and failure degradation."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from career_copilot.adapters.pii.redactor import Redactor
from career_copilot.ports import (
    Detection,
    PiiDetector,
    PiiModelMissingError,
    PiiRedactionError,
)
from career_copilot.usecases.parse_resume import ParsedResume, parse_resume

NOW = datetime(2026, 8, 28, tzinfo=UTC)
NAME = "Anna Schmidt"


class NameDetector:
    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        spans: list[Sequence[Detection]] = []
        for text in texts:
            found: list[Detection] = []
            start = text.find(NAME)
            while start != -1:
                found.append(Detection(start, len(NAME), "PERSON"))
                start = text.find(NAME, start + len(NAME))
            spans.append(found)
        return spans


class BrokenDetector:
    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        raise PiiRedactionError("detector unavailable")


class ModelMissingDetector:
    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        raise PiiModelMissingError("en_core_web_lg")


def parse(text: str, detector: PiiDetector) -> ParsedResume:
    def extract(filename: str, data: bytes) -> str:
        return text

    return parse_resume(
        "cv.pdf",
        b"",
        ["python"],
        NOW,
        extract,
        Redactor(detector=detector, entities=["PERSON"]),
    )


def test_no_redactor_reports_disabled_and_no_preview() -> None:
    def extract(filename: str, data: bytes) -> str:
        return f"{NAME} writes Python."

    parsed = parse_resume("cv.pdf", b"", ["python"], NOW, extract, None)
    assert parsed.privacy.enabled is False
    assert parsed.privacy.redacted_preview is None
    assert parsed.privacy.counts == {}


def test_the_preview_carries_the_full_redacted_text_untruncated() -> None:
    # Well past the old 1200-char cap, and the name is placed near the end so a
    # truncation regression would clip it off — the full text must survive intact.
    filler = "python " * 4000
    tail = f"{NAME} and more text after where the old cut used to be"
    parsed = parse(f"{filler}{tail}", NameDetector())
    preview = parsed.privacy.redacted_preview
    assert preview is not None
    assert len(preview) > 1200
    assert preview.endswith("and more text after where the old cut used to be")
    assert "<|PERSON_1|>" in preview


def test_a_detector_failure_keeps_the_parse_and_logs_no_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        parsed = parse(f"{NAME} writes Python.", BrokenDetector())
    assert list(parsed.detections.skills) == ["Python"]
    assert parsed.privacy.enabled is True
    assert parsed.privacy.redacted_preview is None
    # The screen may say why, as long as "why" is a failure class and nothing else.
    assert parsed.privacy.unavailable_reason == "redaction failed (PiiRedactionError)"
    assert NAME not in caplog.text
    assert caplog.records


def test_a_missing_detection_model_is_named_so_the_remedy_is_knowable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one redaction failure with a one-command fix. Reporting it as a bare failure
    class leaves the user staring at "this file could not be scanned" with no next step."""
    with caplog.at_level(logging.WARNING):
        parsed = parse(f"{NAME} writes Python.", ModelMissingDetector())
    assert list(parsed.detections.skills) == ["Python"]
    assert parsed.privacy.enabled is True
    assert parsed.privacy.redacted_preview is None
    assert parsed.privacy.unavailable_reason is not None
    assert "en_core_web_lg" in parsed.privacy.unavailable_reason
    # A model name is configuration, never a detected value.
    assert NAME not in caplog.text


# --- owner anchor disclosure (person-precision design §7) ------------------------------

OWNER_RESUME = f"{NAME}\nanna.schmidt@example.com\nPython engineer."


class OwnerDetector:
    """Finds the name and the email, the way a rule-based recognizer plus NER would."""

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        found: list[Sequence[Detection]] = []
        for text in texts:
            spans: list[Detection] = []
            for value, category in ((NAME, "PERSON"), ("anna.schmidt@example.com", "EMAIL")):
                start = text.find(value)
                while start != -1:
                    spans.append(Detection(start, len(value), category))
                    start = text.find(value, start + len(value))
            found.append(spans)
        return found


def test_the_preview_discloses_the_owner_anchor_confidence() -> None:
    def extract(filename: str, data: bytes) -> str:
        return OWNER_RESUME

    parsed = parse_resume(
        "Anna_Schmidt_CV.pdf",
        b"",
        ["python"],
        NOW,
        extract,
        Redactor(detector=OwnerDetector(), entities=["PERSON", "EMAIL"]),
    )
    assert parsed.privacy.owner_anchor == "confirmed"


def test_the_anchor_level_is_none_when_no_owner_could_be_derived() -> None:
    def extract(filename: str, data: bytes) -> str:
        return "Kafka and Grafana."

    parsed = parse_resume(
        "stack.pdf",
        b"",
        ["python"],
        NOW,
        extract,
        Redactor(detector=NameDetector(), entities=["PERSON"]),
    )
    assert parsed.privacy.owner_anchor == "none"


def test_no_redactor_reports_no_anchor() -> None:
    def extract(filename: str, data: bytes) -> str:
        return OWNER_RESUME

    parsed = parse_resume("cv.pdf", b"", ["python"], NOW, extract, None)
    assert parsed.privacy.owner_anchor == "none"
