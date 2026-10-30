"""Reads a failed fetch: why it failed, and how long the host asked us to stay away.

Lives beside the adapters because it reads `httpx` errors; the state machine that uses the
answer (`domain/source_health.py`) stays free of I/O.
"""

from __future__ import annotations

import httpx

from career_copilot.adapters.sources.ats import BoardNotFound
from career_copilot.domain.source_health import (
    LONG_RETRY_AFTER_SECONDS,
    FailureRead,
    FailureReason,
)


def classify(error: Exception) -> FailureReason:
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        if code in (404, 410):
            return FailureReason.NOT_FOUND
        if code == 429:
            return FailureReason.RATE_LIMITED
        if code in (401, 403):
            return FailureReason.FORBIDDEN
        if code >= 500:
            return FailureReason.SERVER_ERROR
        return FailureReason.BAD_RESPONSE
    if isinstance(error, httpx.TimeoutException):
        return FailureReason.TIMEOUT
    if isinstance(error, httpx.TransportError):
        return FailureReason.UNREACHABLE
    # A payload that is not what the adapter expects (changed shape, not JSON).
    return FailureReason.BAD_RESPONSE


def retry_after_hours(error: Exception) -> float | None:
    """Hours the host asked us to wait, when that is long enough to count as a block."""
    if not isinstance(error, httpx.HTTPStatusError) or error.response.status_code != 429:
        return None
    try:
        seconds = float(error.response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds / 3600 if seconds > LONG_RETRY_AFTER_SECONDS else None


def read_failure(error: Exception) -> FailureRead:
    reason = classify(error)
    return FailureRead(
        reason=reason,
        retry_after_hours=retry_after_hours(error),
        # A removed board says nothing about the service, so it never counts against it.
        counts=not isinstance(error, BoardNotFound) and reason is not FailureReason.NOT_FOUND,
    )
