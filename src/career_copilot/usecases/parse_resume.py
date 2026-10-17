"""Parse an uploaded resume into detections. `ParsedResume` never carries the text itself."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from career_copilot.adapters.pii.redactor import Redaction
from career_copilot.domain.owner_identity import OwnerIdentity
from career_copilot.domain.resume_detect import ResumeDetections, detect
from career_copilot.ports import PiiModelMissingError, ResumeTextExtractor

logger = logging.getLogger(__name__)


class TextRedactor(Protocol):
    """The slice of the concrete redactor this use case needs. A Protocol, so the use
    case depends on the behaviour and not on how the redactor was composed."""

    def redact(self, texts: Sequence[str]) -> Redaction: ...

    def learn(self, text: str, filename: str | None = None) -> OwnerIdentity: ...


@dataclass(frozen=True, slots=True)
class ResumePrivacy:
    """What the upload path can honestly disclose about the redaction boundary.

    `redacted_preview` holds the full redacted resume text, not a snippet — it is `None`
    when no preview exists — redaction is switched off, or the detector failed. It is never
    the original text: the only string that can appear here is one that came back out of a
    successful `redact()` call.
    """

    enabled: bool
    counts: Mapping[str, int] = field(default_factory=dict)
    redacted_preview: str | None = None
    # The owner anchor's confidence level, never its values: disclosing the derived name
    # would put it back into the response the redaction exists to keep it out of.
    owner_anchor: str = "none"
    # Why there is no preview, when there is none. A failure class or a named missing model
    # — never the text, a config value or a detection, so it is safe to show the user.
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedResume:
    """What survives an upload: a filename, a timestamp, and derived detections."""

    filename: str
    parsed_at: datetime
    detections: ResumeDetections
    privacy: ResumePrivacy = field(default_factory=lambda: ResumePrivacy(enabled=False))


def _preview(text: str, filename: str, redactor: TextRedactor | None) -> ResumePrivacy:
    """Redact the extracted text for disclosure. Never raises: a redaction failure must
    not cost the user their parse (the detections are derived locally and were already
    computed), but it must also never downgrade into returning the original text. The
    honest outcome is "redaction is on, no preview for this file", which is what a `None`
    preview says and what the badge renders.
    """
    if redactor is None:
        return ResumePrivacy(enabled=False)
    try:
        # The resume is the only document the owner identity may be derived from, and this
        # is where it is read. The filename is a scoring signal here and nowhere else.
        anchor = redactor.learn(text, filename).confidence
        redaction = redactor.redact([text])
        redacted = redaction.texts[0]
        counts = dict(redaction.counts)
    except PiiModelMissingError as exc:
        # The one failure with a one-command remedy, so it is worth naming: the model name
        # is a configuration constant, not user data.
        logger.warning("resume redaction preview unavailable (%s)", exc)
        return ResumePrivacy(
            enabled=True,
            unavailable_reason=(
                f"the spaCy model {exc.model_name!r} is not installed on this machine"
            ),
        )
    except Exception as exc:
        # Failure class only — never the text, the config, or any detected value (§11).
        logger.warning("resume redaction preview unavailable (%s)", type(exc).__name__)
        return ResumePrivacy(
            enabled=True,
            unavailable_reason=f"redaction failed ({type(exc).__name__})",
        )
    return ResumePrivacy(
        enabled=True,
        counts=counts,
        redacted_preview=redacted,
        owner_anchor=anchor,
    )


def analyse_resume(
    filename: str,
    text: str,
    parsed_at: datetime,
    skill_terms: Sequence[str],
    redactor: TextRedactor | None = None,
) -> ParsedResume:
    """Detect and redact already-extracted text. Pure derivation: the same text gives the
    same answer, which is why a stored CV can be re-analysed on every read instead of
    storing the detections and the redacted preview beside it."""
    return ParsedResume(
        filename=filename,
        parsed_at=parsed_at,
        detections=detect(text, skill_terms),
        privacy=_preview(text, filename, redactor),
    )


def parse_resume(
    filename: str,
    data: bytes,
    skill_terms: Sequence[str],
    now: datetime,
    extract: ResumeTextExtractor,
    redactor: TextRedactor | None = None,
) -> ParsedResume:
    """Extract, detect, redact, and discard. The resume text does not outlive this call.

    Callers that need to keep the text extract it themselves and call `analyse_resume`.
    """
    return analyse_resume(filename, extract(filename, data), now, skill_terms, redactor)
