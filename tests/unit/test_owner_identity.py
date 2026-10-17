"""Derivation of the resume owner's identity (person-precision design §2)."""

from __future__ import annotations

from career_copilot.domain.owner_identity import OwnerIdentity, derive
from career_copilot.ports import Detection


def det(start: int, length: int, category: str) -> Detection:
    return Detection(start=start, length=length, category=category)


def spans_for(text: str, *values: tuple[str, str]) -> list[Detection]:
    """Locate each (value, category) pair in `text` by literal search."""
    return [det(text.index(value), len(value), category) for value, category in values]


# --- the name: evidence, not position -------------------------------------------------


def test_email_local_part_cross_check_beats_an_earlier_unrelated_person() -> None:
    text = "Reference: Ada Lovelace.\nMarlena Voss\nmarlenavoss97@example.com"
    spans = spans_for(
        text,
        ("Ada Lovelace", "PERSON"),
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
    )
    identity = derive(text, spans)
    assert identity.names == ("Marlena Voss",)
    assert identity.confidence == "confirmed"


def test_filename_confirms_the_name_when_the_text_has_no_email() -> None:
    text = "Summary of work.\nMarlena Voss\nBerlin"
    spans = spans_for(text, ("Marlena Voss", "PERSON"))
    identity = derive(text, spans, filename="Marlena_Voss_Resume.pdf")
    assert identity.names == ("Marlena Voss",)
    assert identity.confidence == "confirmed"


def test_a_name_in_a_footer_wins_on_the_same_evidence_as_one_at_the_top() -> None:
    body = "Grace Hopper wrote the first compiler. " + "Work history. " * 40
    text = body + "\nMarlena Voss — marlenavoss97@example.com"
    spans = spans_for(
        text,
        ("Grace Hopper", "PERSON"),
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
    )
    identity = derive(text, spans)
    assert identity.names == ("Marlena Voss",)
    assert identity.confidence == "confirmed"


def test_a_proximity_only_win_reports_likely() -> None:
    text = "Jane Roe\ncontact@example.com"
    spans = spans_for(text, ("Jane Roe", "PERSON"), ("contact@example.com", "EMAIL"))
    identity = derive(text, spans)
    assert identity.names == ("Jane Roe",)
    assert identity.confidence == "likely"


def test_a_position_only_win_reports_likely() -> None:
    text = "Jane Roe is an engineer. " + "Filler prose. " * 40 + "Ada Lovelace also appears."
    spans = spans_for(text, ("Jane Roe", "PERSON"), ("Ada Lovelace", "PERSON"))
    identity = derive(text, spans)
    assert identity.names == ("Jane Roe",)
    assert identity.confidence == "likely"


def test_no_multi_token_candidate_reports_none() -> None:
    text = "Kafka and Grafana."
    spans = spans_for(text, ("Kafka", "PERSON"), ("Grafana", "PERSON"))
    identity = derive(text, spans)
    assert identity == OwnerIdentity()
    assert identity.confidence == "none"


def test_single_token_spans_are_never_name_candidates() -> None:
    text = "Kafka\nmarlenavoss97@example.com"
    spans = spans_for(text, ("Kafka", "PERSON"), ("marlenavoss97@example.com", "EMAIL"))
    identity = derive(text, spans)
    assert identity.names == ()
    assert identity.confidence == "none"


def test_short_name_tokens_do_not_corroborate_through_the_email() -> None:
    """`Jo` is two characters; the ≥3-character rule keeps it from matching `jones@`."""
    text = "Jo Ng\njones@example.com"
    spans = spans_for(text, ("Jo Ng", "PERSON"), ("jones@example.com", "EMAIL"))
    identity = derive(text, spans)
    assert identity.confidence == "likely"


# --- contacts: every one of them, anywhere ---------------------------------------------


def test_every_email_and_phone_anchors_regardless_of_position() -> None:
    text = "Marlena Voss marlenavoss97@example.com +49 151 2345678\nReferee: ada@x.io"
    spans = spans_for(
        text,
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
        ("+49 151 2345678", "PHONE"),
        ("ada@x.io", "EMAIL"),
    )
    identity = derive(text, spans)
    # The referee address is anchored too, and that is intended: an email address is never
    # scoring evidence, so over-reach here costs nothing (design §2).
    assert identity.emails == ("marlenavoss97@example.com", "ada@x.io")
    assert identity.phones == ("+49 151 2345678",)


def test_contacts_anchor_even_when_no_name_is_derived() -> None:
    text = "Kafka\ncontact@example.com"
    spans = spans_for(text, ("Kafka", "PERSON"), ("contact@example.com", "EMAIL"))
    identity = derive(text, spans)
    assert identity.confidence == "none"
    assert identity.emails == ("contact@example.com",)


def test_a_repeated_contact_value_is_anchored_once() -> None:
    text = "a@x.io writes.\nReach me at a@x.io"
    spans = [*spans_for(text, ("a@x.io", "EMAIL")), det(text.rindex("a@x.io"), 6, "EMAIL")]
    assert derive(text, spans).emails == ("a@x.io",)


# --- determinism ------------------------------------------------------------------------


def test_the_same_text_yields_the_same_identity_across_repeated_calls() -> None:
    text = "Marlena Voss\nmarlenavoss97@example.com\nAda Lovelace"
    spans = spans_for(
        text,
        ("Marlena Voss", "PERSON"),
        ("marlenavoss97@example.com", "EMAIL"),
        ("Ada Lovelace", "PERSON"),
    )
    assert derive(text, spans) == derive(text, spans)


def test_a_derived_contact_is_trimmed_of_punctuation_the_span_swallowed() -> None:
    """Presidio's phone span includes the bracket in `Email (+4915112345678)`, and an
    anchor built from that value would place a second, near-duplicate placeholder."""
    text = "Email (+4915112345678) and <a@x.io>"
    spans = spans_for(text, ("(+4915112345678", "PHONE"), ("<a@x.io>", "EMAIL"))
    identity = derive(text, spans)
    assert identity.phones == ("+4915112345678",)
    assert identity.emails == ("a@x.io",)
