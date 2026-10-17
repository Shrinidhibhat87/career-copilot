"""Derive the resume owner's identity from the resume itself (person-precision design §2).

Pure and deterministic: text, an optional filename, and already-computed detection spans in,
an `OwnerIdentity` out. No I/O and no model call, because an anchor that returns a different
owner on a retry is not an anchor.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from career_copilot.ports import Detection

Confidence = Literal["confirmed", "likely", "none"]

# A name token shorter than this is too weak to corroborate anything: `Jo` would match
# `jones@`, and initials would match almost any local part.
MIN_TOKEN_CHARS = 3

# Contact blocks cluster, wherever they sit on the page. Wide enough to span a header block
# with a title line between the name and the address, narrow enough to exclude body prose.
PROXIMITY_CHARS = 200

# Signals are named rather than compared by weight: proximity and filename happen to score
# the same, and only one of them corroborates.
_WEIGHTS = {"email": 3, "filename": 2, "proximity": 2, "earliest": 1}

# Corroboration by a deterministically found value. A candidate scoring on either of these
# is `confirmed`; one that wins on position alone can only ever be `likely`.
_CONFIRMING = frozenset({"email", "filename"})

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Brackets and quotes a detector span can swallow: `Email (+49...)` yields a PHONE span
# starting at the bracket, and an anchor built from that value would then place a second,
# near-duplicate placeholder next to the one the detector already produced.
_EDGE_PUNCTUATION = " \t\n\r()[]{}<>\"'`,;:|"


@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    """The values the redactor anchors on. Empty tuples plus `none` is a valid outcome."""

    names: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    confidence: Confidence = "none"


def _values(text: str, spans: Sequence[Detection], category: str) -> tuple[str, ...]:
    """Every distinct span text of one category, in order of first appearance."""
    seen: dict[str, None] = {}
    for span in sorted(spans, key=lambda s: s.start):
        if span.category == category:
            seen.setdefault(text[span.start : span.end].strip(_EDGE_PUNCTUATION), None)
    return tuple(value for value in seen if value)


def _name_tokens(name: str) -> list[str]:
    return [token.lower() for token in _WORD.findall(name) if len(token) >= MIN_TOKEN_CHARS]


def _local_parts(emails: Sequence[str]) -> list[str]:
    return [email.split("@", 1)[0].lower() for email in emails if "@" in email]


def _candidates(text: str, spans: Sequence[Detection]) -> list[tuple[Detection, str]]:
    """Multi-token `PERSON` spans. A single token is a product name far more often than an
    owner (design §4), so it never nominates an identity."""
    found = []
    for span in sorted(spans, key=lambda s: s.start):
        if span.category != "PERSON":
            continue
        value = text[span.start : span.end].strip()
        if len(_WORD.findall(value)) >= 2:
            found.append((span, value))
    return found


def _score(
    span: Detection,
    name: str,
    *,
    local_parts: Sequence[str],
    filename: str,
    contacts: Sequence[Detection],
    earliest: int,
) -> frozenset[str]:
    tokens = _name_tokens(name)
    signals: set[str] = set()
    if any(token in part for token in tokens for part in local_parts):
        signals.add("email")
    if filename and any(token in filename for token in tokens):
        signals.add("filename")
    if any(
        contact.start - PROXIMITY_CHARS <= span.start <= contact.end + PROXIMITY_CHARS
        for contact in contacts
    ):
        signals.add("proximity")
    if span.start == earliest:
        signals.add("earliest")
    return frozenset(signals)


def derive(
    text: str,
    spans: Sequence[Detection],
    filename: str | None = None,
) -> OwnerIdentity:
    """Learn the owner's name, emails, and phones from one resume.

    Contacts are taken wholesale: `EMAIL` and `PHONE` come from rule-based recognizers, and
    over-reaching onto a referee's address costs nothing because an address is never scoring
    evidence. Only the name is inferred, and only from checkable signals.
    """
    emails = _values(text, spans, "EMAIL")
    phones = _values(text, spans, "PHONE")

    candidates = _candidates(text, spans)
    if not candidates:
        return OwnerIdentity(emails=emails, phones=phones, confidence="none")

    contacts = [span for span in spans if span.category in {"EMAIL", "PHONE"}]
    local_parts = _local_parts(emails)
    normalized_filename = filename.lower() if filename else ""
    earliest = candidates[0][0].start

    scored = [
        (
            _score(
                span,
                name,
                local_parts=local_parts,
                filename=normalized_filename,
                contacts=contacts,
                earliest=earliest,
            ),
            span.start,
            name,
        )
        for span, name in candidates
    ]
    # Highest total wins; ties break on the earliest offset.
    signals, _, winner = min(
        scored, key=lambda item: (-sum(_WEIGHTS[name] for name in item[0]), item[1])
    )
    if not signals:
        return OwnerIdentity(emails=emails, phones=phones, confidence="none")

    confirmed = bool(signals & _CONFIRMING)
    return OwnerIdentity(
        names=(winner,),
        emails=emails,
        phones=phones,
        confidence="confirmed" if confirmed else "likely",
    )
