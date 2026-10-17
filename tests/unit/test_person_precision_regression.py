"""The regression this slice exists for: the exact excerpt from `BACKLOG.md`.

A policy-level unit test alone would pass while the shipped product still over-redacts, so
this one goes through `Redactor` end to end with the real Presidio detector.
"""

from __future__ import annotations

import logging

import pytest

from career_copilot.adapters.pii.factory import build_redactor
from career_copilot.config import Config, PiiSpec

# The point of this file is what the shipped detector does, so it runs the real model that
# `Config` defaults to rather than a stub. Like `test_presidio_detector.py`, an environment
# without the model skips cleanly instead of failing: CI does not install it.
pytest.importorskip("en_core_web_lg")

# The resume paragraph that produced <|PERSON_3|> / <|PERSON_4|> / <|PERSON_5|>.
EXCERPT = (
    "Marlena Voss — marlenavoss97@example.com — +49 151 23456789\n\n"
    "Co-architected a Kafka-driven ingestion path at EnablerMinds, owned the Kafka "
    "consumer edge and Grafana observability, and moved event streams (Kafka, Event Hubs) "
    "onto Redis and Dragonfly with Apify collectors feeding Langfuse traces."
)

TECHNICAL_NAMES = ("Kafka", "Grafana", "Event Hubs", "Redis", "Dragonfly", "Apify", "Langfuse")


@pytest.fixture(scope="module")
def redacted() -> str:
    redactor = build_redactor(Config())
    assert redactor is not None
    redactor.learn(EXCERPT, "Marlena_Voss_Resume.pdf")
    return redactor.redact([EXCERPT]).texts[0]


@pytest.mark.parametrize("term", TECHNICAL_NAMES)
def test_every_technical_name_survives_literally(redacted: str, term: str) -> None:
    assert term in redacted


def test_the_owner_identity_is_gone(redacted: str) -> None:
    assert "Marlena" not in redacted
    assert "marlenavoss97" not in redacted
    assert "23456789" not in redacted


def test_a_real_two_token_name_in_the_same_input_is_still_replaced() -> None:
    redactor = build_redactor(Config())
    assert redactor is not None
    redactor.learn(EXCERPT, "Marlena_Voss_Resume.pdf")
    text = f"{EXCERPT}\nReviewed by Ada Lovelace of the platform team."
    redacted = redactor.redact([text]).texts[0]
    assert "Ada Lovelace" not in redacted
    assert "Kafka" in redacted


def test_the_derived_identity_is_confirmed_and_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Scoped to this project's own loggers: Presidio's DEBUG stream dumps its context
    # tokens, and that is its logging to answer for, not the boundary's.
    with caplog.at_level(logging.DEBUG, logger="career_copilot"):
        redactor = build_redactor(Config(pii=PiiSpec()))
        assert redactor is not None
        identity = redactor.learn(EXCERPT, "Marlena_Voss_Resume.pdf")
        redactor.redact([EXCERPT])
    assert identity.confidence == "confirmed"
    logged = caplog.text
    for value in ("Marlena", "marlenavoss97", "23456789"):
        assert value not in logged
