"""The redaction boundary: a decorator on the `LLMClient` port (design doc §4, §5, §8).

Every provider call in the process goes through here. Detection and replacement finish
before the inner client is reached; any pre-call failure blocks the call rather than
falling back to raw text; and the original -> placeholder map is a local variable inside
`complete()` that dies when the call returns (§5, invariants 3 and 4).

Wrapping `complete()` rather than the transport also covers the two-attempt retry loop
inside `LiteLLMClient.complete` for free: the retry re-sends an already-redacted
conversation, so there is no second redaction pass and no leak on retry.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from threading import Lock
from typing import Literal

from career_copilot.adapters.pii.redactor import Redaction, Redactor
from career_copilot.ports import (
    LLMClient,
    LLMResponse,
    PiiRedactionError,
    ResponseValidator,
)

log = logging.getLogger(__name__)


class RedactingLLMClient:
    """`ports.LLMClient` decorator that redacts every text argument on the way out and
    restores placeholders on the way back — including inside the caller's validator."""

    def __init__(
        self,
        inner: LLMClient,
        redactor: Redactor,
        outbound: Literal["allow", "warn", "block"] = "warn",
    ) -> None:
        self._inner = inner
        self._redactor = redactor
        self._outbound = outbound
        # Calls can arrive from several worker threads; the analyzer is not documented as
        # thread-safe and its cost is small beside a network call.
        self._lock = Lock()

    @property
    def model(self) -> str:
        """Proxy the wrapped client's model name.

        `MatchAgent.model` reads this off its client and `ScoreRun` uses it to pick which
        jobs still need scoring, so the decorator must be transparent here or the run would
        stamp scores with "unknown".
        """
        return str(getattr(self._inner, "model", "unknown"))

    def complete(
        self,
        prompt: str,
        schema: dict[str, object],
        system: str | None = None,
        validator: ResponseValidator | None = None,
    ) -> LLMResponse:
        # §9: an argument shape the redactor cannot scan is rejected, not forwarded. Only
        # non-`str` text values are expressible in this signature today. When a message part
        # is added later — streaming, a tool/function-call spec, tool results, image or file
        # parts — it must be rejected here too until a scanning path exists for it, because
        # the failure mode otherwise is a silent bypass of the whole gateway.
        if not isinstance(prompt, str):
            raise PiiRedactionError("prompt is not text; the redactor cannot scan it")
        if system is not None and not isinstance(system, str):
            raise PiiRedactionError("system is not text; the redactor cannot scan it")

        # One map for both texts, so the same value gets the same placeholder in the
        # system prompt and the user prompt. Local, never stored, never logged (§5.4).
        with self._lock:
            redaction = self._redactor.redact([prompt, system or ""])

        def restoring(data: dict[str, object]) -> None:
            # The model reads redacted text, so it quotes redacted text. `MatchAgent`'s
            # verbatim-evidence check runs against the *original* resume, so restoration has
            # to happen here, inside the retry loop's callback, not only on the return value.
            if validator is not None:
                validator(_as_data(redaction.restore(data)))

        response = self._inner.complete(
            redaction.texts[0], schema, redaction.texts[1] or None, restoring
        )
        # Placeholder-integrity (§8) lives in `Redaction.restore`; residue raises
        # `PiiRedactionError` from here and fails the response closed.
        restored = _as_data(redaction.restore(response.data))
        self._check_outbound(restored, redaction)
        return replace(response, data=restored)

    def _check_outbound(self, data: dict[str, object], redaction: Redaction) -> None:
        """Monitor model-produced PII without turning scanner failure into a gate."""
        if self._outbound == "allow":
            return
        strings = _string_leaves(data)
        if not strings:
            return
        try:
            with self._lock:
                scanned = self._redactor.detect(strings)
        except Exception as exc:
            log.warning(
                "outbound PII scan unavailable (%s); response returned unscanned",
                type(exc).__name__,
            )
            return

        found: set[str] = set()
        for text, spans in zip(strings, scanned, strict=True):
            for span in spans:
                value = text[span.start : span.end]
                if not redaction.contains_original(value):
                    found.add(span.category)
        if not found:
            return
        categories = ", ".join(sorted(found))
        if self._outbound == "block":
            raise PiiRedactionError(f"model response contains unmapped PII: {categories}")
        log.warning("model response contains unmapped PII categories: %s", categories)


def _as_data(value: object) -> dict[str, object]:
    """`Redaction.restore` walks `str | list | dict` and is typed `object -> object`; a dict
    in is always a dict out. Checked rather than cast so a future change fails loudly."""
    if not isinstance(value, dict):
        raise PiiRedactionError("restoration did not return an object")
    return {str(key): item for key, item in value.items()}


def _string_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _string_leaves(item)]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    return []


# Static check that the runtime class satisfies the port.
_: type[LLMClient] = RedactingLLMClient
