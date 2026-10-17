"""Condense the intent log: draft one note from the active entries, then swap it in.

Two steps with the user in between. The draft is only text the user reads and edits;
nothing in the store changes until they approve, and approval archives the old entries
and adds the note together, so the agent never reads both or neither.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from career_copilot.adapters.pii.redactor import Redaction
from career_copilot.agents.condenser import condense_prompt
from career_copilot.agents.recording import AgentRecording
from career_copilot.domain.intent import (
    MAX_CONDENSED_CHARS,
    IntentEntry,
    IntentKind,
    NothingToCondenseError,
)
from career_copilot.ports import IntentStore

# One answer, plus room for a retry pydantic-ai makes on its own.
CONDENSER_LIMITS = UsageLimits(request_limit=3)
CONDENSER_TIMEOUT_SECONDS = 300.0

# Redacts a batch of texts with one shared placeholder map, keeping the map for restore.
# `None` when `pii.enabled` is false.
Redact = Callable[[Sequence[str]], Redaction]


class CondenseFailedError(Exception):
    """The draft could not be produced: redaction, the model call, or its answer failed."""


class InvalidCondensedError(ValueError):
    """The approved note is empty or too long to store."""


class CondenseStore(IntentStore, Protocol):
    """The intent log, plus the transaction that makes the swap all-or-nothing."""

    def transaction(self) -> AbstractContextManager[None]: ...


def draft_condensed(
    store: IntentStore,
    agent_factory: Callable[[], Agent[None, str]],
    redact: Redact | None,
    timeout: float = CONDENSER_TIMEOUT_SECONDS,
    recording: AgentRecording | None = None,
) -> str:
    """One condensed note drafted from the active entries, in the user's own words.

    Unlike the profiler this does not swallow a failure: the user asked for the draft and
    is waiting on it, so a failure is theirs to see, as `CondenseFailedError`.
    """
    entries = store.active_intent()
    if not entries:
        raise NothingToCondenseError("there are no entries to condense")
    texts = [entry.text for entry in entries]
    try:
        # A pydantic-ai agent bypasses `RedactingLLMClient`, so the gateway runs here.
        redaction = redact(texts) if redact is not None else None
        prompt = condense_prompt(list(redaction.texts) if redaction else texts)
        if recording is not None:
            recording.start()
        result = asyncio.run(
            asyncio.wait_for(
                agent_factory().run(prompt, usage_limits=CONDENSER_LIMITS), timeout=timeout
            )
        )
        if recording is not None:
            recording.finish(result)
        # Restored so the user edits their own words, not placeholders they never wrote.
        draft = str(redaction.restore(result.output)) if redaction else result.output
    except Exception as exc:
        if recording is not None:
            recording.finish(None, "timeout" if isinstance(exc, TimeoutError) else "error")
        # Deliberately broad: a refused connection surfaces as the HTTP client's own error,
        # a timeout as asyncio's, and the caller only needs to know it did not work.
        raise CondenseFailedError(str(exc) or type(exc).__name__) from exc
    draft = draft.strip()
    if not draft:
        raise CondenseFailedError("the model returned an empty note")
    return draft


def approve_condensed(store: CondenseStore, text: str, now: datetime) -> IntentEntry:
    """Archive every active entry and add `text` as the condensed note, in one go."""
    note = text.strip()
    if not note:
        raise InvalidCondensedError("write the condensed note before approving it")
    if len(note) > MAX_CONDENSED_CHARS:
        raise InvalidCondensedError(f"keep the note under {MAX_CONDENSED_CHARS} characters")
    with store.transaction():
        for entry in store.active_intent():
            store.archive_intent(entry.id, now)
        return store.add_intent(note, now, IntentKind.CONDENSED)
