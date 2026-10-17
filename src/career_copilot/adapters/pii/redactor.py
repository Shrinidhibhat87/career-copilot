"""Placeholder-based PII redaction and restoration (design doc §7.3, §8)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from career_copilot.adapters.pii.person_policy import PersonPolicy
from career_copilot.domain.owner_identity import OwnerIdentity, derive
from career_copilot.ports import Detection, PiiDetector, PiiRedactionError

_KNOWN_CATEGORIES = frozenset(
    {"PERSON", "EMAIL", "PHONE", "ADDRESS", "URL", "IBAN", "CREDIT_CARD", "IP"}
)

# Placeholders are always well-formed `<|CATEGORY_N|>`. Any leftover opening delimiter
# after restoration — truncated, mis-cased, or an index never issued — is residue.
_PLACEHOLDER_OPEN = "<|"


def _validate(detection: Detection, text: str, allowed: set[str]) -> Detection | None:
    if detection.start < 0:
        raise PiiRedactionError("a detection has a negative start offset")
    if detection.length <= 0:
        raise PiiRedactionError("a detection has a non-positive length")
    if detection.end > len(text):
        raise PiiRedactionError("a detection extends past the end of its text")
    if detection.category not in _KNOWN_CATEGORIES:
        raise PiiRedactionError("a detection has an unknown category")
    if detection.category not in allowed:
        return None
    return detection


def _merge(detections: list[Detection]) -> list[Detection]:
    """Sort and merge strictly overlapping spans; adjacency is not overlap. Widest wins
    the merged span's category."""
    if not detections:
        return []
    ordered = sorted(detections, key=lambda d: d.start)
    merged: list[Detection] = []
    group_start, group_end = ordered[0].start, ordered[0].end
    widest = ordered[0]
    for detection in ordered[1:]:
        if detection.start < group_end:
            group_end = max(group_end, detection.end)
            if detection.length > widest.length:
                widest = detection
        else:
            merged.append(Detection(group_start, group_end - group_start, widest.category))
            group_start, group_end, widest = detection.start, detection.end, detection
    merged.append(Detection(group_start, group_end - group_start, widest.category))
    return merged


def _apply(text: str, spans: Sequence[Detection], placeholder_of: dict[str, str]) -> str:
    """Replace right to left so earlier offsets stay valid as length changes."""
    result = text
    for detection in sorted(spans, key=lambda d: d.start, reverse=True):
        original = text[detection.start : detection.end]
        result = result[: detection.start] + placeholder_of[original] + result[detection.end :]
    return result


@dataclass(frozen=True, slots=True)
class Redaction:
    """Result of one `Redactor.redact()` call. The reversal map is local to this object
    and is never exposed except through `restore` (§5 invariant 4)."""

    texts: Sequence[str]
    counts: dict[str, int]
    _originals: dict[str, str] = field(repr=False)  # placeholder -> original value

    def restore(self, value: object) -> object:
        """Substitute placeholders back to originals; walks str | list | dict (§8)."""
        if isinstance(value, str):
            return self._restore_str(value)
        if isinstance(value, list):
            return [self.restore(item) for item in value]
        if isinstance(value, dict):
            return {key: self.restore(item) for key, item in value.items()}
        return value

    def contains_original(self, value: str) -> bool:
        """Whether a detected response value came from this request's reversal map."""
        return value in self._originals.values()

    def _restore_str(self, text: str) -> str:
        result = text
        for placeholder in sorted(self._originals, key=len, reverse=True):
            result = result.replace(placeholder, self._originals[placeholder])
        if _PLACEHOLDER_OPEN in result:
            raise PiiRedactionError("restoration left placeholder-shaped residue in the output")
        return result


class _IdentityStore:
    """The one piece of mutable state in the redaction path. It holds derived identity
    values only — never the placeholder map — so design doc §5 invariant 4 still holds."""

    __slots__ = ("_identity", "_overrides")

    def __init__(self, overrides: OwnerIdentity | None = None) -> None:
        self._overrides = overrides or OwnerIdentity()
        self._identity = self._overrides

    def learn(self, text: str, filename: str | None, spans: Sequence[Detection]) -> OwnerIdentity:
        self._identity = _union(derive(text, spans, filename), self._overrides)
        return self._identity

    def values(self) -> OwnerIdentity:
        return self._identity


def _merge_values(derived: tuple[str, ...], pinned: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*derived, *pinned)))


def _union(derived: OwnerIdentity, pinned: OwnerIdentity) -> OwnerIdentity:
    """Pinned values are added to the derived ones, never substituted for them.

    A file the user wrote by hand states what they know to be true, so a pinned name is
    treated as corroboration in its own right — the level it reports is `confirmed`.
    """
    if pinned == OwnerIdentity():
        return derived
    confidence = "confirmed" if pinned.names else derived.confidence
    return OwnerIdentity(
        names=_merge_values(derived.names, pinned.names),
        emails=_merge_values(derived.emails, pinned.emails),
        phones=_merge_values(derived.phones, pinned.phones),
        confidence=confidence,
    )


@dataclass(frozen=True, slots=True)
class Redactor:
    """Detects and replaces PII across a batch of texts with one shared placeholder
    mapping, so the same value gets the same token everywhere in the call."""

    detector: PiiDetector
    entities: Sequence[str]
    policy: PersonPolicy | None = None
    owner_anchor: bool = True
    overrides: OwnerIdentity | None = None
    _store: _IdentityStore = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_store", _IdentityStore(self.overrides))

    def learn(self, text: str, filename: str | None = None) -> OwnerIdentity:
        """Derive the owner's identity from the resume, once, before any scoring call.

        Deriving per-call would be wrong: a scoring prompt is the resume plus a posting, and
        a recruiter named in the posting would compete with the owner for the anchor.
        """
        if not self.owner_anchor:
            return OwnerIdentity()
        spans = self.detector.detect([text])[0]
        if self.policy is not None:
            # Filtered spans, never raw ones. Presidio nominates `Confluent Kafka` as a
            # PERSON and a grade/year run as a PHONE; fed to derivation those outscore the
            # real values and the owner's own name would go out unredacted.
            spans = self.policy.filter(text, spans, OwnerIdentity())
        return self._store.learn(text, filename, spans)

    def identity(self) -> OwnerIdentity:
        """The derived identity. Confidence level is disclosable; the values are not."""
        return self._store.values()

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        """Scan and validate text using the same policy as outbound redaction.

        Order matters. The precision filter runs on what the detector returned, and the
        owner anchor is unioned in *afterwards*, so an anchored value can never be dropped
        by the filter. That ordering is what expresses provenance — `Detection` needs no
        `source` field and the `PiiDetector` port is unchanged.
        """
        detections = self.detector.detect(texts)
        if len(detections) != len(texts):
            raise PiiRedactionError("detector result count does not match input count")
        allowed = set(self.entities)
        return [
            [
                span
                for span in (_validate(item, text, allowed) for item in self._policed(text, spans))
                if span
            ]
            for text, spans in zip(texts, detections, strict=True)
        ]

    def _policed(self, text: str, spans: Sequence[Detection]) -> list[Detection]:
        if self.policy is None:
            return list(spans)
        identity = self._store.values()
        return [
            *self.policy.filter(text, spans, identity),
            *self.policy.anchor_spans(text, identity),
        ]

    def redact(self, texts: Sequence[str]) -> Redaction:
        detections = self.detect(texts)
        spans_per_text = [_merge(list(spans)) for spans in detections]

        # Number placeholders in order of first appearance across the whole sequence,
        # before any replacement happens.
        placeholder_of: dict[str, str] = {}
        original_of: dict[str, str] = {}
        counters: dict[str, int] = {}
        for text, spans in zip(texts, spans_per_text, strict=True):
            for detection in spans:
                original = text[detection.start : detection.end]
                if original in placeholder_of:
                    continue
                counters[detection.category] = counters.get(detection.category, 0) + 1
                placeholder = f"<|{detection.category}_{counters[detection.category]}|>"
                placeholder_of[original] = placeholder
                original_of[placeholder] = original

        redacted_texts = [
            _apply(text, spans, placeholder_of)
            for text, spans in zip(texts, spans_per_text, strict=True)
        ]
        return Redaction(texts=redacted_texts, counts=dict(counters), _originals=original_of)
