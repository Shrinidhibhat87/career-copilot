from __future__ import annotations

from collections.abc import Sequence

import pytest

from career_copilot.adapters.pii.redactor import Redaction, Redactor
from career_copilot.ports import Detection, PiiRedactionError


class StubDetector:
    """Hand-written detector returning fixed `Detection`s per call, keyed by input order."""

    def __init__(self, results: Sequence[Sequence[Detection]]) -> None:
        self._results = results

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        return self._results


ALL_ENTITIES = ("PERSON", "EMAIL", "PHONE", "ADDRESS", "URL", "IBAN", "CREDIT_CARD", "IP")


def det(start: int, length: int, category: str) -> Detection:
    return Detection(start=start, length=length, category=category)


# --- Redactor: placeholder numbering, sharing, collisions ---------------------------


def test_placeholder_numbering_is_per_category_in_first_appearance_order() -> None:
    text = "Ada wrote to Grace, ada@x.io, grace@y.io"
    detections = [
        det(0, 3, "PERSON"),  # Ada
        det(13, 5, "PERSON"),  # Grace
        det(20, 8, "EMAIL"),  # ada@x.io
        det(30, 10, "EMAIL"),  # grace@y.io
    ]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    assert redaction.texts[0] == ("<|PERSON_1|> wrote to <|PERSON_2|>, <|EMAIL_1|>, <|EMAIL_2|>")
    assert redaction.counts == {"PERSON": 2, "EMAIL": 2}


def test_repeated_value_shares_one_token() -> None:
    text = "Ada met Ada"
    detections = [det(0, 3, "PERSON"), det(8, 3, "PERSON")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    assert redaction.texts[0] == "<|PERSON_1|> met <|PERSON_1|>"
    assert redaction.counts == {"PERSON": 1}


def test_distinct_values_never_collide() -> None:
    text = "Ada and Ida"
    detections = [det(0, 3, "PERSON"), det(8, 3, "PERSON")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    assert redaction.texts[0] == "<|PERSON_1|> and <|PERSON_2|>"


def test_one_shared_mapping_across_multiple_input_texts() -> None:
    texts = ["Ada is here", "Ada wrote back, Grace replied"]
    detections = [
        [det(0, 3, "PERSON")],
        [det(0, 3, "PERSON"), det(16, 5, "PERSON")],
    ]
    redactor = Redactor(StubDetector(detections), ALL_ENTITIES)
    redaction = redactor.redact(texts)
    assert redaction.texts[0] == "<|PERSON_1|> is here"
    assert redaction.texts[1] == "<|PERSON_1|> wrote back, <|PERSON_2|> replied"
    assert redaction.counts == {"PERSON": 2}


# --- Merge behaviour ------------------------------------------------------------------


def test_right_to_left_application_two_spans_in_one_string() -> None:
    text = "Ada emailed ada@x.io yesterday"
    detections = [det(0, 3, "PERSON"), det(12, 8, "EMAIL")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    assert redaction.texts[0] == "<|PERSON_1|> emailed <|EMAIL_1|> yesterday"


def test_strictly_overlapping_spans_merge_widest_wins() -> None:
    text = "Dr. Ada Lovelace works here"
    # PERSON "Ada Lovelace" (4..16) overlaps a shorter, wrongly-tagged EMAIL span (4..7).
    detections = [det(4, 12, "PERSON"), det(4, 3, "EMAIL")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    assert redaction.texts[0] == "Dr. <|PERSON_1|> works here"
    assert redaction.counts == {"PERSON": 1}


def test_adjacent_spans_do_not_merge() -> None:
    text = "Ada<ada@x.io"
    # PERSON "Ada" ends exactly where EMAIL "ada@x.io" begins: touching, not overlapping.
    detections = [det(0, 3, "PERSON"), det(4, 8, "EMAIL")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    assert redaction.texts[0] == "<|PERSON_1|><<|EMAIL_1|>"
    assert redaction.counts == {"PERSON": 1, "EMAIL": 1}


# --- Empty / whitespace input -----------------------------------------------------------


def test_empty_string_input() -> None:
    redactor = Redactor(StubDetector([[]]), ALL_ENTITIES)
    redaction = redactor.redact([""])
    assert redaction.texts == [""]
    assert redaction.counts == {}


def test_whitespace_only_input() -> None:
    redactor = Redactor(StubDetector([[]]), ALL_ENTITIES)
    redaction = redactor.redact(["   \n\t  "])
    assert redaction.texts == ["   \n\t  "]
    assert redaction.counts == {}


# --- Category filtering ---------------------------------------------------------------


def test_known_category_outside_allowlist_is_dropped_silently() -> None:
    text = "Ada emailed ada@x.io"
    detections = [det(0, 3, "PERSON"), det(12, 8, "EMAIL")]
    redactor = Redactor(StubDetector([detections]), entities=("PERSON",))
    redaction = redactor.redact([text])
    assert redaction.texts[0] == "<|PERSON_1|> emailed ada@x.io"
    assert redaction.counts == {"PERSON": 1}


# --- Span validation: fail closed ------------------------------------------------------


def test_negative_start_raises() -> None:
    redactor = Redactor(StubDetector([[det(-1, 3, "PERSON")]]), ALL_ENTITIES)
    with pytest.raises(PiiRedactionError):
        redactor.redact(["Ada"])


def test_zero_length_raises() -> None:
    redactor = Redactor(StubDetector([[det(0, 0, "PERSON")]]), ALL_ENTITIES)
    with pytest.raises(PiiRedactionError):
        redactor.redact(["Ada"])


def test_negative_length_raises() -> None:
    redactor = Redactor(StubDetector([[det(0, -1, "PERSON")]]), ALL_ENTITIES)
    with pytest.raises(PiiRedactionError):
        redactor.redact(["Ada"])


def test_end_past_text_length_raises() -> None:
    redactor = Redactor(StubDetector([[det(0, 10, "PERSON")]]), ALL_ENTITIES)
    with pytest.raises(PiiRedactionError):
        redactor.redact(["Ada"])


def test_unknown_category_raises() -> None:
    redactor = Redactor(StubDetector([[det(0, 3, "SSN")]]), ALL_ENTITIES)
    with pytest.raises(PiiRedactionError):
        redactor.redact(["Ada"])


def test_result_list_cardinality_mismatch_raises() -> None:
    redactor = Redactor(StubDetector([[], []]), ALL_ENTITIES)
    with pytest.raises(PiiRedactionError):
        redactor.redact(["one text only"])


def test_span_validation_error_message_has_no_source_text_or_mapped_value() -> None:
    redactor = Redactor(StubDetector([[det(-1, 3, "PERSON")]]), ALL_ENTITIES)
    with pytest.raises(PiiRedactionError) as exc_info:
        redactor.redact(["TopSecretName"])
    assert "TopSecretName" not in str(exc_info.value)


# --- restore: longest-replacement-first, nested structures ----------------------------


def test_restore_longest_replacement_first_when_one_replacement_prefixes_another() -> None:
    # Adversarial: PERSON_1's own original value is literally the text "<|PERSON_10|>",
    # and PERSON_10 is a genuine, separately-issued placeholder mapping to "Zoe".
    # Restoring PERSON_1 produces the byproduct string "<|PERSON_10|>" in the output.
    # If restoration processed the shorter placeholder first, that byproduct would then
    # get matched by the later PERSON_10 substitution and silently become "Zoe" — a
    # corrupted, wrong restoration. Longest-replacement-first processes PERSON_10 before
    # PERSON_1, so the byproduct is never re-substituted; it is instead correctly caught
    # as placeholder-shaped residue by the integrity check.
    redaction = Redaction(
        texts=(),
        counts={"PERSON": 2},
        _originals={
            "<|PERSON_1|>": "<|PERSON_10|>",
            "<|PERSON_10|>": "Zoe",
        },
    )
    with pytest.raises(PiiRedactionError) as exc_info:
        redaction.restore("start <|PERSON_1|> end")
    assert "Zoe" not in str(exc_info.value)


def test_restore_walks_nested_list_and_dict() -> None:
    text = "Ada emailed ada@x.io"
    detections = [det(0, 3, "PERSON"), det(12, 8, "EMAIL")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    redacted = redaction.texts[0]
    payload = {"notes": [redacted, {"quote": redacted}], "count": 2}
    restored = redaction.restore(payload)
    assert restored == {
        "notes": [text, {"quote": text}],
        "count": 2,
    }


# --- Placeholder integrity --------------------------------------------------------------


@pytest.mark.parametrize(
    "residue",
    [
        "<|PERSON 1|>",
        "<|person_1|>",
        "<|PERSON_7|>",  # never issued
        "<|PERSON_",  # truncated
    ],
)
def test_placeholder_integrity_fails_closed_on_residue(residue: str) -> None:
    text = "Ada is here"
    detections = [det(0, 3, "PERSON")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    tampered = redaction.texts[0] + " " + residue
    with pytest.raises(PiiRedactionError):
        redaction.restore(tampered)


def test_omitted_placeholder_does_not_raise() -> None:
    text = "Ada and Grace"
    detections = [det(0, 3, "PERSON"), det(8, 5, "PERSON")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    # Model dropped the second placeholder entirely from its reply.
    model_reply = "<|PERSON_1|> said hi"
    restored = redaction.restore(model_reply)
    assert restored == "Ada said hi"


def test_restore_error_message_has_no_mapped_value_or_residue_token() -> None:
    text = "Ada is here"
    detections = [det(0, 3, "PERSON")]
    redactor = Redactor(StubDetector([detections]), ALL_ENTITIES)
    redaction = redactor.redact([text])
    tampered = redaction.texts[0] + " <|PERSON_7|>"
    with pytest.raises(PiiRedactionError) as exc_info:
        redaction.restore(tampered)
    message = str(exc_info.value)
    assert "Ada" not in message
    assert "PERSON_7" not in message
    assert "<|" not in message
