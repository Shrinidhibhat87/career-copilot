"""The policy seam inside `Redactor.detect` (person-precision design §3)."""

from __future__ import annotations

from collections.abc import Sequence

from career_copilot.adapters.pii.person_policy import PersonPolicy
from career_copilot.adapters.pii.redactor import Redactor
from career_copilot.ports import Detection, PiiDetector

ALL_ENTITIES = ("PERSON", "EMAIL", "PHONE", "ADDRESS", "URL", "IBAN", "CREDIT_CARD", "IP")

RESUME = (
    "Marlena Voss\nmarlenavoss97@example.com\n+49 151 2345678\n\n"
    "Co-architected a Kafka-driven ingestion path and owned Grafana observability."
)


class LiteralDetector:
    """Finds fixed literals wherever they occur, so a test can state detector output as the
    values a real NER model would (and did) return."""

    def __init__(self, *values: tuple[str, str]) -> None:
        self._values = values

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        found = []
        for text in texts:
            spans = []
            for value, category in self._values:
                start = text.find(value)
                while start != -1:
                    spans.append(Detection(start, len(value), category))
                    start = text.find(value, start + 1)
            found.append(sorted(spans, key=lambda span: span.start))
        return found


def build(
    detector: PiiDetector, learn: str | None = RESUME, filename: str | None = None
) -> Redactor:
    redactor = Redactor(detector, ALL_ENTITIES, policy=PersonPolicy())
    if learn is not None:
        redactor.learn(learn, filename)
    return redactor


def test_single_token_product_names_survive_redaction() -> None:
    detector = LiteralDetector(
        ("Kafka", "PERSON"),
        ("Grafana", "PERSON"),
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
        ("+49 151 2345678", "PHONE"),
    )
    redaction = build(detector).redact([RESUME])
    text = redaction.texts[0]
    assert "Kafka-driven" in text
    assert "Grafana observability" in text
    assert "Marlena" not in text
    assert "marlenavoss97@example.com" not in text


def test_the_owner_name_is_redacted_where_the_detector_never_saw_it() -> None:
    detector = LiteralDetector(
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
        ("+49 151 2345678", "PHONE"),
    )
    redactor = build(detector)
    # The detector is given no PERSON span in this text at all.
    posting = "The reviewer was Marlena Voss, reachable at marlenavoss97@example.com."
    redaction = redactor.redact([posting])
    assert "Marlena" not in redaction.texts[0]
    assert "<|PERSON_1|>" in redaction.texts[0]


def test_an_anchored_single_token_owner_name_is_not_filtered_away() -> None:
    """Anchors are unioned in after the filter, so the filter can never reach them."""
    detector = LiteralDetector(
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
        ("+49 151 2345678", "PHONE"),
    )
    redaction = build(detector).redact(["Ask Marlena about the Kafka consumer."])
    text = redaction.texts[0]
    assert "Marlena" not in text
    assert "Kafka consumer" in text


def test_the_owner_phone_is_redacted_across_a_separator_variant() -> None:
    detector = LiteralDetector(
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
        ("+49 151 2345678", "PHONE"),
    )
    redaction = build(detector).redact(["Reach me on 0151 2345678 any time."])
    assert "2345678" not in redaction.texts[0]


def test_other_categories_are_untouched_by_the_policy() -> None:
    detector = LiteralDetector(("https://x.io", "URL"), ("10.0.0.1", "IP"))
    redaction = build(detector, learn=None).redact(["See https://x.io on 10.0.0.1"])
    assert redaction.counts == {"URL": 1, "IP": 1}


def test_without_a_policy_detection_is_unchanged() -> None:
    detector = LiteralDetector(("Kafka", "PERSON"))
    redactor = Redactor(detector, ALL_ENTITIES)
    assert redactor.redact(["Kafka streams"]).texts[0] == "<|PERSON_1|> streams"


def test_learning_reports_the_confidence_level() -> None:
    detector = LiteralDetector(
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
    )
    identity = build(detector).learn(RESUME, "Marlena_Voss_Resume.pdf")
    assert identity.confidence == "confirmed"
    assert identity.names == ("Marlena Voss",)


def test_identity_is_learned_from_the_resume_not_from_each_redacted_text() -> None:
    """A recruiter named in a posting must not compete with the owner for the anchor."""
    detector = LiteralDetector(
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
        ("Ada Lovelace", "PERSON"),
        ("ada.lovelace@acme.test", "EMAIL"),
    )
    redactor = build(detector)
    redactor.redact(["Contact Ada Lovelace at ada.lovelace@acme.test about the role."])
    assert redactor.identity().names == ("Marlena Voss",)


def test_learning_ignores_spans_the_policy_would_have_dropped() -> None:
    """Derivation runs on filtered spans, not raw ones.

    Presidio returns `Confluent Kafka` as a PERSON and a grade/year run as a PHONE. Fed to
    derivation unfiltered, the product name wins the anchor — the owner's real name is then
    never redacted, which is the exact failure the anchor exists to prevent.
    """
    resume = (
        "Marlena Voss marlenavoss97@example.com +49 151 2345 6789\n"
        "Event streams (Confluent Kafka, Event Hubs). Grade: 9.06/10.0 2019"
    )
    detector = LiteralDetector(
        ("Marlena Voss", "PERSON"),
        ("Confluent Kafka", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
        ("+49 151 2345 6789", "PHONE"),
        ("9.06/10.0 2019", "PHONE"),
    )
    identity = Redactor(detector, ALL_ENTITIES, policy=PersonPolicy()).learn(resume)
    assert identity.names == ("Marlena Voss",)
    assert identity.phones == ("+49 151 2345 6789",)
    assert identity.confidence == "confirmed"
