from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from career_copilot.adapters.pii import presidio_detector as pd_module
from career_copilot.adapters.pii.presidio_detector import PresidioDetector
from career_copilot.ports import Detection, PiiRedactionError

# `en_core_web_sm` is the small model: fast to load, good enough for the exact-name PERSON
# cases below. It is a real dependency (not just data), so `pytest.importorskip` gives an
# offline machine a clean skip instead of a hard failure (task brief).
spacy_model = pytest.importorskip("en_core_web_sm")

DETECTOR = PresidioDetector(model_name="en_core_web_sm", language="en")


def spans(text: str) -> list[Detection]:
    return list(DETECTOR.detect([text])[0])


def literal(text: str, detection: Detection) -> str:
    return text[detection.start : detection.end]


# --- rule-based recognizers: no NER quality required ---------------------------------------


def test_email_is_mapped_to_email_category() -> None:
    text = "Reach me at ada@example.com for details."
    found = [d for d in spans(text) if d.category == "EMAIL"]
    assert len(found) == 1
    assert literal(text, found[0]) == "ada@example.com"


def test_phone_is_mapped_to_phone_category() -> None:
    text = "Call +49 151 23456789 during business hours."
    found = [d for d in spans(text) if d.category == "PHONE"]
    assert len(found) == 1
    assert literal(text, found[0]) == "+49 151 23456789"


def test_iban_is_mapped_to_iban_category() -> None:
    text = "Transfer to DE89370400440532013000 please."
    found = [d for d in spans(text) if d.category == "IBAN"]
    assert len(found) == 1
    assert literal(text, found[0]) == "DE89370400440532013000"


def test_credit_card_is_mapped_to_credit_card_category() -> None:
    text = "Card number: 4111111111111111."
    found = [d for d in spans(text) if d.category == "CREDIT_CARD"]
    assert len(found) == 1
    assert literal(text, found[0]) == "4111111111111111"


def test_ip_is_mapped_to_ip_category() -> None:
    text = "Origin IP 192.168.1.1 logged."
    found = [d for d in spans(text) if d.category == "IP"]
    assert len(found) == 1
    assert literal(text, found[0]) == "192.168.1.1"


def test_url_is_mapped_to_url_category() -> None:
    text = "See https://example.com/jobs for the posting."
    found = [d for d in spans(text) if d.category == "URL"]
    assert len(found) >= 1
    assert any(literal(text, d) == "https://example.com/jobs" for d in found)


def test_person_is_mapped_to_person_category() -> None:
    text = "My name is John Smith."
    found = [d for d in spans(text) if d.category == "PERSON"]
    assert len(found) == 1
    assert literal(text, found[0]) == "John Smith"


# --- street address recognizer --------------------------------------------------------------


def test_street_address_fires_on_german_ordering() -> None:
    text = "I live at Musterstraße 12, 10115 Berlin, in case that matters."
    found = [d for d in spans(text) if d.category == "ADDRESS"]
    assert len(found) == 1
    assert literal(text, found[0]) == "Musterstraße 12, 10115 Berlin"


def test_street_address_fires_on_english_ordering() -> None:
    text = "Send it to 12 Baker Street, thanks."
    found = [d for d in spans(text) if d.category == "ADDRESS"]
    assert len(found) == 1
    assert literal(text, found[0]) == "12 Baker Street"


def test_street_address_fires_on_house_number_prefixed_ordering() -> None:
    text = "The building at 221B Baker Street is fictional."
    found = [d for d in spans(text) if d.category == "ADDRESS"]
    assert len(found) == 1
    assert literal(text, found[0]) == "221B Baker Street"


def test_street_address_fires_on_ring_suffix_with_postcode() -> None:
    # "-ring" needs the mandatory postcode + city tail (fix round 2) to be distinguishable
    # from a gerund ("Spring", "Hiring", "Monitoring") in bare prose.
    text = "Send mail to Sachsenring 12, 50733 Köln please."
    found = [d for d in spans(text) if d.category == "ADDRESS"]
    assert len(found) == 1
    assert literal(text, found[0]) == "Sachsenring 12, 50733 Köln"


def test_street_address_fires_on_chaussee_suffix() -> None:
    # "chaussee" only matches in its compound single-word form, same architectural limit as
    # every other suffix in the list (see the residual-gap note in the report) — there is no
    # realistic two-word "<Adjective> Chaussee" positive here for that reason.
    text = "Meet me at Ackerstraßenchaussee 1, 10115 Berlin tomorrow."
    found = [d for d in spans(text) if d.category == "ADDRESS"]
    assert len(found) == 1
    assert literal(text, found[0]) == "Ackerstraßenchaussee 1, 10115 Berlin"


def test_street_address_fires_on_damm_suffix() -> None:
    # Kurfürstendamm is a real, well-known Berlin street and a genuine compound word ending
    # in "damm" — a stronger, non-fictional positive than the chaussee case above.
    text = "The office is on Kurfürstendamm 15, 10719 Berlin."
    found = [d for d in spans(text) if d.category == "ADDRESS"]
    assert len(found) == 1
    assert literal(text, found[0]) == "Kurfürstendamm 15, 10719 Berlin"


# --- street address recognizer: known false-positive shapes must NOT fire -------------------
# Verified live against the reviewer's counter-examples (fix round 1): the bare "ring"
# suffix in the German list matched the tail of any capitalised "-ring" word, and the bare
# English abbreviations ("St", "Ct", "Dr", ...) plus the bare word "Way" collided with
# ordinary capitalised prose. Job postings are HTML-derived prose, so this is not a
# hypothetical input domain. Fix round 2 restored "-ring" behind a mandatory postcode + city
# requirement (see test_street_address_fires_on_ring_suffix_with_postcode above); these five
# cases below have no postcode/city tail, so they still cannot match with "-ring" back in the
# alternation.


@pytest.mark.parametrize(
    "text",
    [
        "Applications open Spring 2026 for this role.",
        "We are Hiring 5 engineers this quarter.",
        "Monitoring 24 dashboards daily.",
        "We follow a 3 Step Way to onboard new hires.",
        "5 Star Ct rating on Glassdoor.",
    ],
)
def test_street_address_recognizer_does_not_fire_on_prose_false_positives(text: str) -> None:
    found = [d for d in spans(text) if d.category == "ADDRESS"]
    assert found == []


# --- LOCATION is deliberately not redacted (§7.1) --------------------------------------------


def test_location_city_is_not_redacted() -> None:
    text = "Berlin is a great city. I love working in Berlin."
    found = [d for d in spans(text) if d.category in {"ADDRESS", "LOCATION"}]
    assert found == []


# --- allowlist filtering: an unmapped Presidio entity produces no Detection -----------------


def test_unmapped_presidio_entity_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Finding:
        entity_type = "US_DRIVER_LICENSE"
        start = 0
        end = 5

    class _StubAnalyzer:
        def analyze(self, text: str, language: str) -> list[_Finding]:
            return [_Finding()]

    detector = PresidioDetector(model_name="en_core_web_sm", language="en")
    detector._analyzer = _StubAnalyzer()  # bypass lazy construction for this stub
    assert detector.detect(["12345"]) == [[]]


# --- offsets, multiplicity, ordering ----------------------------------------------------------


def test_offsets_are_exact() -> None:
    text = "Email ada@example.com now."
    (found,) = [d for d in spans(text) if d.category == "EMAIL"]
    assert text[found.start : found.end] == "ada@example.com"


def test_value_occurring_twice_yields_two_spans() -> None:
    text = "ada@example.com and again ada@example.com."
    found = [d for d in spans(text) if d.category == "EMAIL"]
    assert len(found) == 2
    assert all(literal(text, d) == "ada@example.com" for d in found)


def test_multiple_texts_return_results_positionally() -> None:
    texts = [
        "Contact ada@example.com.",
        "No PII in this sentence at all.",
        "Call +49 151 23456789 please.",
    ]
    results = DETECTOR.detect(texts)
    assert len(results) == 3
    assert any(d.category == "EMAIL" for d in results[0])
    assert not any(d.category in {"EMAIL", "PHONE"} for d in results[1])
    assert any(d.category == "PHONE" for d in results[2])


# --- fail-closed behaviour ---------------------------------------------------------------------


def test_analyzer_construction_failure_raises_pii_redaction_error() -> None:
    detector = PresidioDetector(model_name="not-a-real-spacy-model", language="en")
    with pytest.raises(PiiRedactionError) as exc_info:
        detector.detect(["hello"])
    # The message names the failure class, never analysed text (design doc §11).
    assert "hello" not in str(exc_info.value)


def test_analysis_failure_raises_pii_redaction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomAnalyzer:
        def analyze(self, text: str, language: str) -> Sequence[Any]:
            raise RuntimeError("boom")

    detector = PresidioDetector(model_name="en_core_web_sm", language="en")
    detector._analyzer = _BoomAnalyzer()
    with pytest.raises(PiiRedactionError) as exc_info:
        detector.detect(["secret text that must never leak"])
    assert "secret text that must never leak" not in str(exc_info.value)
    assert "boom" not in str(exc_info.value)


def test_detector_does_not_build_analyzer_until_first_detect_call() -> None:
    # Regression guard for the lazy-construction requirement: a freshly constructed
    # detector has not built an analyzer yet.
    detector = PresidioDetector()
    assert detector._analyzer is None
    assert pd_module is not None


def test_importing_the_module_never_loads_spacy() -> None:
    # Importing this module transitively happens from both the FastAPI app and the CLI;
    # neither should pay spaCy's ~1s model-load cost just to start (ambiguity resolution
    # from the task brief). Run in a subprocess so the current test session's already-loaded
    # `spacy` module can't mask a regression.
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import career_copilot.adapters.pii.presidio_detector; "
            "assert 'spacy' not in sys.modules, sorted(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
