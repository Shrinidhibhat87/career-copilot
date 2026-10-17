"""Build the search profile a run scores against, or reuse the one already built.

A profile is an aid, never a precondition: with nothing written yet there is nothing to
profile, and a failed or timed-out model call keeps the last good profile. A 4B model must
not be able to break a run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from career_copilot.agents.profiler import ProfileDraft, profiler_prompt
from career_copilot.agents.recording import AgentRecording
from career_copilot.domain.cv import StoredCv
from career_copilot.domain.search_profile import SearchProfile, inputs_hash
from career_copilot.ports import IntentStore, SampleRoleStore, SearchProfileStore

log = logging.getLogger(__name__)

# One structured answer plus the retries a malformed reply earns.
PROFILER_LIMITS = UsageLimits(request_limit=3)
PROFILER_TIMEOUT_SECONDS = 300.0

# Redacts a batch of texts with one shared placeholder map.
Redact = Callable[[Sequence[str]], Sequence[str]]


class ProfileSources(IntentStore, SampleRoleStore, SearchProfileStore, Protocol):
    """What the profiler reads and where it caches what it wrote."""


def ensure_profile(
    store: ProfileSources,
    agent: Callable[[], Agent[None, ProfileDraft]],
    cv: StoredCv | None,
    redact: Redact,
    model: str,
    now: datetime,
    timeout: float = PROFILER_TIMEOUT_SECONDS,
    recording: AgentRecording | None = None,
) -> SearchProfile | None:
    """The profile for the current inputs. `agent` is a factory, so a run with nothing to
    profile, or a cached profile, never builds a model client at all."""
    entries = store.active_intent()
    roles = store.active_sample_roles()
    if not entries and not roles:
        return None
    key = inputs_hash(entries, roles, cv.sha256 if cv else None)
    cached = store.search_profile(key)
    if cached is not None:
        return cached
    try:
        # The CV and the user's own words go through the same gateway as every scoring
        # call; a pydantic-ai agent bypasses `RedactingLLMClient`, so it happens here.
        private = [cv.text if cv else "", *(entry.text for entry in entries)]
        cleaned = list(redact(private))
        prompt = profiler_prompt(cleaned[1:], roles, cleaned[0])
        if recording is not None:
            recording.start()
        result = asyncio.run(
            asyncio.wait_for(agent().run(prompt, usage_limits=PROFILER_LIMITS), timeout=timeout)
        )
        if recording is not None:
            recording.finish(result)
    except Exception as exc:
        if recording is not None:
            recording.finish(None, "timeout" if isinstance(exc, TimeoutError) else "error")
        # Deliberately broad: a refused connection surfaces as the HTTP client's own error,
        # not pydantic-ai's, and no profiler failure is worth a failed run.
        log.warning("profiler failed, keeping the last profile: %s", exc)
        return store.latest_search_profile()
    profile = result.output.to_profile()
    if not profile.summary:
        return store.latest_search_profile()
    store.save_search_profile(key, profile, model, now)
    return profile
