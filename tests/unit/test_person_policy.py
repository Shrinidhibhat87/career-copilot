"""PERSON precision policy: the filter and the owner anchor (person-precision design §4)."""

from __future__ import annotations

from career_copilot.adapters.pii.person_policy import PersonPolicy
from career_copilot.domain.owner_identity import OwnerIdentity
from career_copilot.ports import Detection

NONE = OwnerIdentity()
CONFIRMED = OwnerIdentity(names=("Marlena Voss",), confidence="confirmed")


def spans_for(text: str, *values: tuple[str, str]) -> list[Detection]:
    return [Detection(text.index(value), len(value), category) for value, category in values]


def kept(
    text: str,
    spans: list[Detection],
    policy: PersonPolicy,
    identity: OwnerIdentity = NONE,
) -> list[str]:
    """The span texts that survive as detections — i.e. the values that get redacted."""
    return [text[s.start : s.end] for s in policy.filter(text, spans, identity)]


# --- single-token drop -----------------------------------------------------------------


def test_single_token_person_false_positives_are_dropped() -> None:
    text = "Kafka, Grafana, Apify and Dragonfly with Ada Lovelace."
    spans = spans_for(
        text,
        ("Kafka", "PERSON"),
        ("Grafana", "PERSON"),
        ("Apify", "PERSON"),
        ("Dragonfly", "PERSON"),
        ("Ada Lovelace", "PERSON"),
    )
    assert kept(text, spans, PersonPolicy(), CONFIRMED) == ["Ada Lovelace"]


def test_trailing_punctuation_and_hyphenation_still_count_as_one_token() -> None:
    text = "Kafka. A Kafka-driven pipeline."
    spans = spans_for(text, ("Kafka.", "PERSON"), ("Kafka-driven", "PERSON"))
    assert kept(text, spans, PersonPolicy(), CONFIRMED) == []


def test_the_single_token_drop_can_be_switched_off() -> None:
    text = "Kafka"
    spans = spans_for(text, ("Kafka", "PERSON"))
    policy = PersonPolicy(drop_single_token_person=False)
    assert kept(text, spans, policy, CONFIRMED) == ["Kafka"]


def test_the_drop_is_suspended_near_contact_spans_when_no_owner_was_derived() -> None:
    """A mononym owner beside their own contact details survives at `none` (design §2)."""
    text = "Marlena\ncontact@example.com\n\n" + "Body prose. " * 40 + "Kafka streams."
    spans = spans_for(
        text,
        ("Marlena", "PERSON"),
        ("contact@example.com", "EMAIL"),
        ("Kafka", "PERSON"),
    )
    assert kept(text, spans, PersonPolicy(), NONE) == ["Marlena", "contact@example.com"]


def test_the_suspension_does_not_apply_once_an_owner_is_confirmed() -> None:
    text = "Marlena\ncontact@example.com"
    spans = spans_for(text, ("Marlena", "PERSON"), ("contact@example.com", "EMAIL"))
    assert kept(text, spans, PersonPolicy(), CONFIRMED) == ["contact@example.com"]


# --- term exemptions --------------------------------------------------------------------


def test_a_two_token_product_name_is_exempt_by_term() -> None:
    text = "Event Hubs and Ada Lovelace"
    spans = spans_for(text, ("Event Hubs", "PERSON"), ("Ada Lovelace", "PERSON"))
    policy = PersonPolicy(false_positive_terms=frozenset({"event hubs"}))
    assert kept(text, spans, policy, CONFIRMED) == ["Ada Lovelace"]


def test_exemption_matches_the_whole_normalized_span_not_a_substring() -> None:
    """`kafka` in the list must not exempt a real person who happens to be named Kafka."""
    text = "Kafka Rodrigues joined."
    spans = spans_for(text, ("Kafka Rodrigues", "PERSON"))
    policy = PersonPolicy(false_positive_terms=frozenset({"kafka"}))
    assert kept(text, spans, policy, CONFIRMED) == ["Kafka Rodrigues"]


def test_exemption_normalizes_case_and_internal_whitespace() -> None:
    text = "EVENT   Hubs"
    spans = spans_for(text, ("EVENT   Hubs", "PERSON"))
    policy = PersonPolicy(false_positive_terms=frozenset({"event hubs"}))
    assert kept(text, spans, policy, CONFIRMED) == []


# --- every other category passes through ------------------------------------------------


def test_no_other_category_is_ever_suppressed() -> None:
    text = "a@x.io 00491512345 Berlin http://x.io DE89 4111 10.0.0.1"
    spans = [
        Detection(0, 6, "EMAIL"),
        Detection(7, 11, "PHONE"),
        Detection(19, 6, "ADDRESS"),
        Detection(26, 11, "URL"),
        Detection(38, 4, "IBAN"),
        Detection(43, 4, "CREDIT_CARD"),
        Detection(48, 8, "IP"),
    ]
    policy = PersonPolicy(false_positive_terms=frozenset({"berlin", "a@x.io"}))
    assert len(policy.filter(text, spans, CONFIRMED)) == len(spans)


# --- anchoring ---------------------------------------------------------------------------


def test_the_owner_name_is_anchored_where_the_detector_missed_it() -> None:
    text = "Reviewed by marlena  voss in 2024."
    identity = OwnerIdentity(names=("Marlena Voss",), confidence="confirmed")
    anchored = PersonPolicy().anchor_spans(text, identity)
    assert [(text[s.start : s.end], s.category) for s in anchored] == [("marlena  voss", "PERSON")]


def test_a_bare_first_name_is_anchored_when_the_email_corroborates_it() -> None:
    text = "Ask Marlena about it."
    identity = OwnerIdentity(
        names=("Marlena Voss",),
        emails=("marlenavoss97@example.com",),
        confidence="confirmed",
    )
    anchored = PersonPolicy().anchor_spans(text, identity)
    assert ("Marlena", "PERSON") in [(text[s.start : s.end], s.category) for s in anchored]


def test_a_name_token_absent_from_the_email_is_not_anchored_alone() -> None:
    text = "Ask Marlena about it."
    identity = OwnerIdentity(
        names=("Marlena Voss",), emails=("contact@example.com",), confidence="likely"
    )
    assert PersonPolicy().anchor_spans(text, identity) == []


def test_an_email_is_anchored_case_insensitively() -> None:
    text = "Write to MarlenaVoss97@Example.com today."
    identity = OwnerIdentity(emails=("marlenavoss97@example.com",), confidence="confirmed")
    anchored = PersonPolicy().anchor_spans(text, identity)
    assert [(text[s.start : s.end], s.category) for s in anchored] == [
        ("MarlenaVoss97@Example.com", "EMAIL")
    ]


def test_a_phone_is_anchored_across_separator_variants() -> None:
    text = "Call +49151 234 56 78 or 0151 2345678."
    identity = OwnerIdentity(phones=("+49 151 2345678",), confidence="confirmed")
    anchored = PersonPolicy().anchor_spans(text, identity)
    found = [text[s.start : s.end].strip() for s in anchored]
    assert "+49151 234 56 78" in found
    assert "0151 2345678" in found
    assert all(s.category == "PHONE" for s in anchored)


def test_a_short_digit_run_is_not_mistaken_for_the_owner_phone() -> None:
    text = "Shipped 2345678 events in 2024."
    identity = OwnerIdentity(phones=("+49 151 999 000 11",), confidence="confirmed")
    assert PersonPolicy().anchor_spans(text, identity) == []


def test_anchoring_is_empty_when_no_identity_was_derived() -> None:
    assert PersonPolicy().anchor_spans("Kafka and Grafana", OwnerIdentity()) == []


# --- vendor-qualified product names ------------------------------------------------------


def test_a_vendor_qualified_product_name_is_dropped() -> None:
    """`Confluent Kafka` is two tokens, so the structural single-token rule cannot see it,
    and whole-span equality means the `kafka` entry does not cover it either."""
    text = "Event streams (Confluent Kafka, Event Hubs) and Apache Kafka."
    spans = spans_for(text, ("Confluent Kafka", "PERSON"), ("Apache Kafka", "PERSON"))
    policy = PersonPolicy(false_positive_terms=frozenset({"kafka"}))
    assert kept(text, spans, policy, CONFIRMED) == []


def test_a_real_name_ending_in_a_product_word_is_the_accepted_cost() -> None:
    """Stated rather than hidden: the qualifier rule keys on the last token, so a person
    named after a product loses recall. The owner is anchored, so this can only ever cost a
    third party — the same trade §5 of the design already takes."""
    text = "Kafka Rodrigues joined."
    spans = spans_for(text, ("Kafka Rodrigues", "PERSON"))
    policy = PersonPolicy(false_positive_terms=frozenset({"kafka"}))
    assert kept(text, spans, policy, CONFIRMED) == ["Kafka Rodrigues"]


# --- phone spans that are not phone numbers ---------------------------------------------


def test_a_decimal_number_is_not_a_phone_number() -> None:
    """Presidio reads `9.06/10.0 2019` — a grade and a year — as a PHONE_NUMBER."""
    text = "Grade: 9.06/10.0 2019 - 2021"
    spans = spans_for(text, ("9.06/10.0 2019", "PHONE"))
    assert kept(text, spans, PersonPolicy(), CONFIRMED) == []


def test_a_digit_run_too_short_to_dial_is_not_a_phone_number() -> None:
    text = "Room 2019"
    spans = spans_for(text, ("2019", "PHONE"))
    assert kept(text, spans, PersonPolicy(), CONFIRMED) == []


def test_a_real_phone_number_still_survives() -> None:
    text = "Call +49 151 2345 6789 or 030/1234567."
    spans = spans_for(text, ("+49 151 2345 6789", "PHONE"), ("030/1234567", "PHONE"))
    assert kept(text, spans, PersonPolicy(), CONFIRMED) == [
        "+49 151 2345 6789",
        "030/1234567",
    ]
