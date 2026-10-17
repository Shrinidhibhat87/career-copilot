"""Union the always-on detection floor with an optional recall pass."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Literal

from career_copilot.ports import Detection, PiiDetector, PiiRedactionError

log = logging.getLogger(__name__)


class HybridDetector:
    """Run both detectors; optionally tolerate only the secondary failing."""

    def __init__(
        self,
        primary: PiiDetector,
        secondary: PiiDetector,
        on_secondary_failure: Literal["block", "primary_only"] = "block",
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._on_secondary_failure = on_secondary_failure
        self.degraded = False

    def warm(self) -> None:
        """Warm the local floor; the secondary is a remote model with nothing to build."""
        warm = getattr(self._primary, "warm", None)
        if callable(warm):
            warm()

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        originals = tuple(texts)
        try:
            primary = self._primary.detect(originals)
        except Exception as exc:
            raise PiiRedactionError(
                f"the PII detection floor failed ({type(exc).__name__})"
            ) from exc

        try:
            secondary: Sequence[Sequence[Detection]] = self._secondary.detect(originals)
        except Exception as exc:
            if self._on_secondary_failure == "block":
                raise PiiRedactionError(
                    f"the PII recall pass failed ({type(exc).__name__})"
                ) from exc
            self.degraded = True
            log.warning(
                "PII recall pass unavailable (%s); continuing with the detection floor",
                type(exc).__name__,
            )
            secondary = [[] for _ in originals]

        if len(primary) != len(originals) or len(secondary) != len(originals):
            raise PiiRedactionError("a PII detector returned the wrong result count")
        return [
            sorted(set(first) | set(second), key=lambda span: (span.start, span.length))
            for first, second in zip(primary, secondary, strict=True)
        ]
