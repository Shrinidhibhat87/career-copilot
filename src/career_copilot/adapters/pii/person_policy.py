"""PERSON precision: drop statistical false positives, anchor the owner's own identity.

The two halves are deliberately separate and deliberately ordered (person-precision design
§3). `filter` removes `PERSON` spans that are almost certainly product names; `anchor_spans`
adds the owner's values back from the text directly, *after* the filter has run, so an
anchored value can never be filtered away.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from career_copilot.domain.filters import term_pattern
from career_copilot.domain.owner_identity import (
    MIN_TOKEN_CHARS,
    PROXIMITY_CHARS,
    OwnerIdentity,
)
from career_copilot.ports import Detection

# Products that are not scoring skills, so they are not in `filters.core_stack_terms` and
# would otherwise need a config entry each. Two-token entries are the ones that matter:
# a single token is already handled structurally.
_PRODUCT_TERMS = frozenset(
    {
        "grafana",
        "apify",
        "redis",
        "dragonfly",
        "event hubs",
        "langfuse",
        "presidio",
    }
)

_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# A digit run with the separators a phone number is written with, long enough that a year
# or an item count cannot reach it.
_PHONE_CANDIDATE = re.compile(r"[+(]?\d[\d\s().\-/]{5,}\d")
_MIN_PHONE_DIGITS = 7
# A decimal point between digits. No dialable number contains one, and Presidio reads
# `9.06/10.0 2019` — a grade followed by a year — as a phone number.
_DECIMAL = re.compile(r"\d\.\d")


def _normalize(value: str) -> str:
    return _WHITESPACE.sub(" ", value.strip()).lower()


def _is_single_token(value: str) -> bool:
    """One token means no internal whitespace once the edges are stripped. `Kafka.` and
    `Kafka-driven` are one token; `Event Hubs` is two."""
    return not _WHITESPACE.search(value.strip().strip(".,;:!?()[]\"'"))


def _digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _phone_key(digits: str) -> str:
    """National trunk prefixes are written as a leading zero and dropped when the country
    code is present, so they cannot take part in the suffix comparison."""
    return digits.lstrip("0")


def _is_dialable(value: str) -> bool:
    """Whether a `PHONE` span can be a phone number at all.

    The one place a non-`PERSON` category is dropped, and deliberately narrow. Presidio's
    phone recognizer fires on grade/year runs like `9.06/10.0 2019`, which then show up in
    the disclosure badge as a phone number the resume does not contain. Both rules are
    structural: no dialable number carries a decimal point, and none is shorter than seven
    digits. Every other category, and every phone that passes these, is untouched.
    """
    return len(_digits(value)) >= _MIN_PHONE_DIGITS and not _DECIMAL.search(value)


def _same_phone(left: str, right: str) -> bool:
    if left == right:
        return True
    a, b = _phone_key(left), _phone_key(right)
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= _MIN_PHONE_DIGITS and longer.endswith(shorter)


@dataclass(frozen=True, slots=True)
class PersonPolicy:
    """Precision rules for `PERSON`. Pure: no I/O, no model, no spaCy."""

    false_positive_terms: frozenset[str] = frozenset()
    drop_single_token_person: bool = True

    def filter(
        self,
        text: str,
        spans: Sequence[Detection],
        identity: OwnerIdentity,
    ) -> list[Detection]:
        """Drop `PERSON` false positives. Every other category passes through untouched.

        `identity` is needed for one rule only: with no derived owner name, the single-token
        drop is suspended near contact details, so a mononym owner beside their own email is
        still caught (design §2).
        """
        exempt = self.false_positive_terms | _PRODUCT_TERMS
        suspended = self._suspension_zones(spans) if identity.confidence == "none" else ()
        return [span for span in spans if self._keeps(text, span, exempt, suspended)]

    def anchor_spans(self, text: str, identity: OwnerIdentity) -> list[Detection]:
        """Every occurrence of the owner's derived values in `text`, as detections."""
        found: list[Detection] = []
        for name in identity.names:
            found.extend(self._name_spans(text, name, identity.emails))
        for email in identity.emails:
            pattern = re.compile(rf"(?<!\w){re.escape(email)}(?!\w)", re.IGNORECASE)
            found.extend(
                Detection(m.start(), m.end() - m.start(), "EMAIL") for m in pattern.finditer(text)
            )
        for phone in identity.phones:
            found.extend(self._phone_spans(text, phone))
        return _dedupe(found)

    # -- internals ---------------------------------------------------------------------

    def _keeps(
        self,
        text: str,
        span: Detection,
        exempt: frozenset[str],
        suspended: Sequence[tuple[int, int]],
    ) -> bool:
        if span.category == "PHONE":
            return _is_dialable(text[span.start : span.end])
        if span.category != "PERSON":
            return True
        value = text[span.start : span.end]
        # Equality of the complete normalized span, never a substring: a substring rule
        # would let `kafka` exempt a real person named `Kafka Rodrigues`.
        normalized = _normalize(value)
        if normalized in exempt:
            return False
        # A vendor-qualified product: `Confluent Kafka`, `Apache Kafka`, `Azure Databricks`.
        # Keying on the last token keeps `Kafka Rodrigues` — a name whose *first* token is
        # the product word — redacted, which is the shape a real person's name takes.
        head, _, last = normalized.rpartition(" ")
        if head and last in exempt:
            return False
        if not self.drop_single_token_person or not _is_single_token(value):
            return True
        return any(start <= span.start <= end for start, end in suspended)

    @staticmethod
    def _suspension_zones(spans: Sequence[Detection]) -> tuple[tuple[int, int], ...]:
        return tuple(
            (span.start - PROXIMITY_CHARS, span.end + PROXIMITY_CHARS)
            for span in spans
            if span.category in {"EMAIL", "PHONE"}
        )

    @staticmethod
    def _name_spans(text: str, name: str, emails: Sequence[str]) -> Iterator[Detection]:
        """The whole name, plus single tokens the owner's own email corroborates.

        The token rule is what catches a body-text `Marlena` with no surname. It is gated
        on the email so that a common given name only anchors when a deterministically found
        value already vouches for it.
        """
        for match in term_pattern(name).finditer(text):
            yield Detection(match.start(), match.end() - match.start(), "PERSON")
        local_parts = [email.split("@", 1)[0].lower() for email in emails if "@" in email]
        for token in _WORD.findall(name):
            if len(token) < MIN_TOKEN_CHARS:
                continue
            if not any(token.lower() in part for part in local_parts):
                continue
            for match in term_pattern(token).finditer(text):
                yield Detection(match.start(), match.end() - match.start(), "PERSON")

    @staticmethod
    def _phone_spans(text: str, phone: str) -> Iterator[Detection]:
        wanted = _digits(phone)
        if len(wanted) < _MIN_PHONE_DIGITS:
            return
        for match in _PHONE_CANDIDATE.finditer(text):
            if _same_phone(_digits(match.group()), wanted):
                yield Detection(match.start(), match.end() - match.start(), "PHONE")


def _dedupe(spans: Sequence[Detection]) -> list[Detection]:
    seen: dict[tuple[int, int, str], Detection] = {}
    for span in spans:
        seen.setdefault((span.start, span.length, span.category), span)
    return sorted(seen.values(), key=lambda span: span.start)
