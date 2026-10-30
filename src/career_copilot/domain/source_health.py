"""Whether a job source (one vendor or service) is worth asking right now.

Pure: a source's history and the clock in, its next state out, with no I/O, so the same
history always gives the same state. Each board still backs off on its own (the cooldowns
table); this is the layer above it, so a service that has changed or banned us stops being
asked by every one of its boards. See spec 017.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

# A host asking for longer than this has blocked us for a while (Workable answers 86400
# after a burst), so the breaker opens at once for as long as it asked.
LONG_RETRY_AFTER_SECONDS = 10.0


class FailureReason(StrEnum):
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    FORBIDDEN = "forbidden"
    SERVER_ERROR = "server_error"
    BAD_RESPONSE = "bad_response"
    # The board is gone (HTTP 404/410): a problem with that board, not with the source.
    NOT_FOUND = "not_found"


# What the run view and the digest say for each reason, in place of a raw error that can be
# long and carry a URL.
REASON_TEXT: dict[str, str] = {
    FailureReason.UNREACHABLE: "could not be reached",
    FailureReason.TIMEOUT: "took too long to answer",
    FailureReason.RATE_LIMITED: "asked us to slow down and is being left alone for a while",
    FailureReason.FORBIDDEN: "refused access",
    FailureReason.SERVER_ERROR: "had an error on its side",
    FailureReason.BAD_RESPONSE: "answered something unexpected",
    FailureReason.NOT_FOUND: "no longer exists (the company may have removed its board)",
}


def explain(reason: str | None) -> str | None:
    """The sentence for a stored reason, or None for a row recorded before reasons existed."""
    return REASON_TEXT.get(reason or "")


@dataclass(frozen=True, slots=True)
class FailureRead:
    """What a failed fetch means for the source it came from."""

    reason: FailureReason = FailureReason.BAD_RESPONSE
    retry_after_hours: float | None = None
    # False for a clean "no such board": the service answered, so it is not sick.
    counts: bool = True


def unread(error: Exception) -> FailureRead:
    """The reader a run uses when none is given: every failure counts, for no stated reason."""
    return FailureRead()


@dataclass(frozen=True, slots=True)
class HealthConfig:
    open_after: int = 3
    pause_hours: int = 24
    max_pause_hours: int = 24 * 7


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source: str
    consecutive_failures: int = 0
    paused_until: datetime | None = None
    pause_hours: int = 24
    last_ok_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure: FailureReason | None = None
    last_message: str | None = None


def blocks(health: SourceHealth | None, now: datetime) -> bool:
    """True while the source is paused. Once the pause ends the next target is the retry."""
    return health is not None and health.paused_until is not None and health.paused_until > now


def status(health: SourceHealth | None, now: datetime) -> str:
    """`unknown` before any fetch, then `paused`, `degraded` (failing, not yet paused) or `ok`."""
    if health is None or (health.last_ok_at is None and health.last_failure_at is None):
        return "unknown"
    if blocks(health, now):
        return "paused"
    if health.consecutive_failures > 0:
        return "degraded"
    return "ok"


def record(
    health: SourceHealth | None,
    source: str,
    ok: bool,
    now: datetime,
    cfg: HealthConfig,
    reason: FailureReason | None = None,
    message: str | None = None,
    retry_after_hours: float | None = None,
) -> SourceHealth:
    """The state after one fetch. A success closes the breaker; a failure may open it."""
    current = health or SourceHealth(source=source, pause_hours=cfg.pause_hours)
    if ok:
        return replace(
            current,
            consecutive_failures=0,
            paused_until=None,
            pause_hours=cfg.pause_hours,
            last_ok_at=now,
        )

    failures = current.consecutive_failures + 1
    pause_hours = current.pause_hours
    paused_until = None
    if retry_after_hours is not None:
        # The service said how long to stay away, so believe it (up to the cap).
        paused_until = now + timedelta(hours=min(retry_after_hours, cfg.max_pause_hours))
    elif current.paused_until is not None and current.paused_until <= now:
        # The retry after a pause failed too: wait twice as long, up to the cap.
        pause_hours = min(pause_hours * 2, cfg.max_pause_hours)
        paused_until = now + timedelta(hours=pause_hours)
    elif failures >= cfg.open_after:
        paused_until = now + timedelta(hours=pause_hours)
    return replace(
        current,
        consecutive_failures=failures,
        paused_until=paused_until,
        pause_hours=pause_hours,
        last_failure_at=now,
        last_failure=reason,
        last_message=(message or "")[:200] or None,
    )


def resume(health: SourceHealth | None, now: datetime) -> SourceHealth | None:
    """A paused source retried on the next run. Nothing is fetched here: the pause is cut
    short, so the next target of the source is the retry, and a failed retry still pauses
    for twice as long. A source that is not paused is returned as it was."""
    if health is None or not blocks(health, now):
        return health
    return replace(health, paused_until=now)
