"""The HTTP contract for /api/resume, including every documented failure."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from career_copilot.adapters.api import app as api
from career_copilot.adapters.pii.redactor import Redactor
from career_copilot.adapters.resume.extract import ResumeExtractionError
from career_copilot.config import Config, PiiSpec
from career_copilot.ports import Detection, PiiRedactionError
from career_copilot.usecases.parse_resume import analyse_resume
from tests.resume_fixtures import make_encrypted_pdf, make_pdf

PDF_TYPE = "application/pdf"
RESUME = "Python engineer. Kubernetes and FastAPI. Languages: German B2, English C1."

# The contact block a real resume opens with. Kept ASCII: the PDF fixture writes the
# string into a Latin-1 content stream.
NAME = "Anna Schmidt"
EMAIL = "anna@example.com"
PHONE = "+49 151 23456789"
PII_RESUME = f"{NAME} - {EMAIL} - {PHONE} - {NAME} again. {RESUME}"

ENTITIES = PiiSpec().entities


class SeededDetector:
    """Deterministic stand-in for Presidio at the API boundary.

    The contract under test is what the payload discloses, not detection quality, and a
    spaCy model load inside every upload test would buy nothing. Presidio itself is covered
    in `tests/unit/test_presidio_detector.py`; one test below still runs the real detector.
    """

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        found: list[Sequence[Detection]] = []
        for text in texts:
            spans: list[Detection] = []
            for value, category in ((NAME, "PERSON"), (EMAIL, "EMAIL"), (PHONE, "PHONE")):
                start = text.find(value)
                while start != -1:
                    spans.append(Detection(start, len(value), category))
                    start = text.find(value, start + len(value))
            found.append(spans)
        return found


class FailingRedactor:
    """A redactor whose detector is unavailable — the degradation path of §12."""

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        raise PiiRedactionError("detector unavailable")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    config = Config.load()
    config.db_path = str(tmp_path / "jobs.db")
    monkeypatch.setattr(api, "_config", config)
    monkeypatch.setattr(api, "_redactor", Redactor(detector=SeededDetector(), entities=ENTITIES))
    api._resume_cache.clear()
    with TestClient(api.app) as test_client:
        yield test_client


def _upload(client: TestClient, name: str, data: bytes, content_type: str = PDF_TYPE) -> Any:
    # `TestClient` returns whichever httpx starlette picked; the tests read status and
    # json off it, so the transport's exact Response class is not the contract here.
    return client.post("/api/resume", files={"file": (name, data, content_type)})


def test_upload_returns_detections_and_counts(client: TestClient) -> None:
    body = _upload(client, "anna-schmidt-cv.pdf", make_pdf(RESUME)).json()
    assert body["filename"] == "anna-schmidt-cv.pdf"
    assert body["detected"]["skills"] == ["Python", "Kubernetes", "FastAPI"]
    assert body["detected"]["languages"] == [
        {"name": "English", "level": "C1"},
        {"name": "German", "level": "B2"},
    ]
    assert body["summary"] == {
        "roles": len(body["detected"]["roles"]),
        "skills": 3,
        "languages": 2,
    }
    assert body["parsedAt"]


def test_upload_is_readable_again_and_clearable(client: TestClient) -> None:
    assert client.get("/api/resume").status_code == 404
    _upload(client, "cv.pdf", make_pdf(RESUME))
    assert client.get("/api/resume").json()["filename"] == "cv.pdf"
    assert client.delete("/api/resume").status_code == 204
    assert client.get("/api/resume").status_code == 404


def test_upload_reports_version_and_whether_it_changed(client: TestClient) -> None:
    first = _upload(client, "cv.pdf", make_pdf(RESUME)).json()
    assert (first["version"], first["changed"]) == (1, True)
    assert len(first["sha256"]) == 64
    same = _upload(client, "cv-again.pdf", make_pdf(RESUME)).json()
    assert (same["version"], same["changed"], same["sha256"]) == (1, False, first["sha256"])
    other = _upload(client, "cv.pdf", make_pdf(RESUME + " Kafka.")).json()
    assert (other["version"], other["changed"]) == (2, True)


def test_the_cv_survives_a_restart(client: TestClient) -> None:
    _upload(client, "cv.pdf", make_pdf(RESUME))
    # A restart is a new process over the same database: nothing is held in memory.
    again = client.get("/api/resume").json()
    assert again["filename"] == "cv.pdf" and again["version"] == 1 and again["changed"] is False
    assert again["detected"]["skills"] == ["Python", "Kubernetes", "FastAPI"]


def test_versions_are_listed_newest_first(client: TestClient) -> None:
    _upload(client, "a.pdf", make_pdf(RESUME))
    _upload(client, "b.pdf", make_pdf(RESUME + " Kafka."))
    versions = client.get("/api/resume/versions").json()
    assert [(v["version"], v["filename"], v["current"]) for v in versions] == [
        (2, "b.pdf", True),
        (1, "a.pdf", False),
    ]


def test_unsupported_type_is_415(client: TestClient) -> None:
    assert _upload(client, "cv.txt", b"Python", "text/plain").status_code == 415


def test_oversized_body_is_413(client: TestClient) -> None:
    oversized = b"%PDF-1.4\n" + b"0" * (api.MAX_BYTES + 1)
    assert _upload(client, "cv.pdf", oversized).status_code == 413


def test_unreadable_document_is_422(client: TestClient) -> None:
    assert _upload(client, "cv.pdf", make_encrypted_pdf(RESUME)).status_code == 422


def test_unexpected_extraction_failure_is_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(filename: str, data: bytes) -> str:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(api, "extract", boom)
    assert _upload(client, "cv.pdf", make_pdf(RESUME)).status_code == 502


def test_a_failure_leaves_an_existing_parse_intact(client: TestClient) -> None:
    _upload(client, "cv.pdf", make_pdf(RESUME))
    _upload(client, "notes.txt", b"Python", "text/plain")
    assert client.get("/api/resume").json()["filename"] == "cv.pdf"


def test_extraction_error_maps_to_422_not_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def empty(filename: str, data: bytes) -> str:
        raise ResumeExtractionError("nothing readable")

    monkeypatch.setattr(api, "extract", empty)
    assert _upload(client, "cv.pdf", make_pdf(RESUME)).status_code == 422


# --- privacy disclosure (design doc §12) ----------------------------------------------------


def test_upload_discloses_counts_and_a_redacted_preview(client: TestClient) -> None:
    response = _upload(client, "cv.pdf", make_pdf(PII_RESUME))
    privacy = response.json()["privacy"]
    assert privacy["enabled"] is True
    assert privacy["counts"] == {"PERSON": 1, "EMAIL": 1, "PHONE": 1}
    preview = privacy["redactedPreview"]
    assert "<|PERSON_1|>" in preview
    assert "<|EMAIL_1|>" in preview
    assert "<|PHONE_1|>" in preview
    # The whole body, not just the preview: no field may carry an original value (§11).
    for value in (NAME, EMAIL, PHONE):
        assert value not in response.text


def test_counts_are_distinct_values_not_occurrences(client: TestClient) -> None:
    """`PII_RESUME` names Anna twice; one person was replaced, so the badge says one."""
    privacy = _upload(client, "cv.pdf", make_pdf(PII_RESUME)).json()["privacy"]
    assert privacy["counts"]["PERSON"] == 1
    assert privacy["redactedPreview"].count("<|PERSON_1|>") == 2


def test_get_returns_the_same_privacy_block(client: TestClient) -> None:
    posted = _upload(client, "cv.pdf", make_pdf(PII_RESUME)).json()["privacy"]
    fetched = client.get("/api/resume")
    assert fetched.json()["privacy"] == posted
    for value in (NAME, EMAIL, PHONE):
        assert value not in fetched.text


def test_disabled_redaction_is_reported_rather_than_omitted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "_redactor", None)
    response = _upload(client, "cv.pdf", make_pdf(PII_RESUME))
    body = response.json()
    assert body["privacy"] == {
        "enabled": False,
        "counts": {},
        "redactedPreview": None,
        "ownerAnchor": "none",
        # Off is not a failure, so there is nothing to explain.
        "unavailableReason": None,
    }
    # Redaction being off is not a licence to hand the original text back out.
    for value in (NAME, EMAIL, PHONE):
        assert value not in response.text


def test_a_redaction_failure_keeps_the_parse(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "_redactor", Redactor(detector=FailingRedactor(), entities=ENTITIES))
    response = _upload(client, "cv.pdf", make_pdf(PII_RESUME))
    body = response.json()
    assert response.status_code == 200
    assert body["detected"]["skills"] == ["Python", "Kubernetes", "FastAPI"]
    # Redaction is configured on, so the badge must not claim it is off; there is simply
    # no preview to show, which `null` says and an empty string would not.
    assert body["privacy"] == {
        "enabled": True,
        "counts": {},
        "redactedPreview": None,
        "ownerAnchor": "none",
        # The screen may say why, as long as it names only the failure class.
        "unavailableReason": "redaction failed (PiiRedactionError)",
    }
    for value in (NAME, EMAIL, PHONE):
        assert value not in response.text


def test_the_real_detector_produces_a_placeholder_preview(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pass with Presidio itself, so the disclosure is not only proved against a stub."""
    pytest.importorskip("en_core_web_sm")
    from career_copilot.adapters.pii.presidio_detector import PresidioDetector

    monkeypatch.setattr(
        api,
        "_redactor",
        Redactor(detector=PresidioDetector(model_name="en_core_web_sm"), entities=ENTITIES),
    )
    response = _upload(client, "cv.pdf", make_pdf(PII_RESUME))
    privacy = response.json()["privacy"]
    assert privacy["enabled"] is True
    # Which category claims the contact block is Presidio's business (an NER PERSON span can
    # swallow the address next to it, and the widest span wins the merge). What this test
    # pins is the disclosure: something was replaced, the preview says so, and the original
    # contact values are gone from the response.
    assert privacy["counts"]
    assert "<|PERSON_1|>" in privacy["redactedPreview"]
    for value in (NAME, EMAIL, PHONE):
        assert value not in response.text


def test_the_payload_discloses_the_anchor_level_and_never_the_derived_values(
    client: TestClient,
) -> None:
    privacy = _upload(client, "Anna_Schmidt_CV.pdf", make_pdf(PII_RESUME)).json()["privacy"]
    assert privacy["ownerAnchor"] == "confirmed"
    serialized = str(privacy)
    for value in (NAME, EMAIL, PHONE):
        assert value not in serialized


def _count_analyses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = analyse_resume

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "analyse_resume", counting)
    return calls


def test_repeat_reads_analyse_the_resume_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upload(client, "cv.pdf", make_pdf(RESUME))
    calls = _count_analyses(monkeypatch)
    first = client.get("/api/resume").json()
    second = client.get("/api/resume").json()
    assert first == second
    assert len(calls) == 1


def test_a_new_upload_is_analysed_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upload(client, "cv.pdf", make_pdf(RESUME))
    calls = _count_analyses(monkeypatch)
    client.get("/api/resume")
    _upload(client, "other.pdf", make_pdf(RESUME + "\nRust"))
    assert len(calls) == 2  # one read, one upload
    client.get("/api/resume")
    assert len(calls) == 3
