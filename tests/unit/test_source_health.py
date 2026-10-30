"""The source-health state machine and the failure reader. Pure: no clock, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from career_copilot.adapters.sources.ats import BoardNotFound
from career_copilot.adapters.sources.failures import classify, read_failure
from career_copilot.domain.source_health import (
    FailureReason,
    HealthConfig,
    SourceHealth,
    blocks,
    record,
    resume,
    status,
)

T0 = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
CFG = HealthConfig(open_after=3, pause_hours=24, max_pause_hours=168)


def fail(health: SourceHealth | None, at: datetime, **kw: object) -> SourceHealth:
    return record(health, "workable", False, at, CFG, reason=FailureReason.TIMEOUT, **kw)  # type: ignore[arg-type]


def pause_end(health: SourceHealth) -> datetime:
    """A minute after the pause ends, when the next fetch is the retry."""
    assert health.paused_until is not None
    return health.paused_until + timedelta(minutes=1)


def test_it_opens_on_the_third_failure_in_a_row_not_the_second() -> None:
    one = fail(None, T0)
    two = fail(one, T0)
    assert not blocks(two, T0) and status(two, T0) == "degraded"
    three = fail(two, T0)
    assert blocks(three, T0) and status(three, T0) == "paused"
    assert three.paused_until == T0 + timedelta(hours=24)


def test_a_success_resets_the_count_and_closes_it() -> None:
    health = fail(fail(None, T0), T0)
    healed = record(health, "workable", True, T0, CFG)
    assert healed.consecutive_failures == 0 and healed.paused_until is None
    assert status(healed, T0) == "ok"


def test_the_next_target_after_the_pause_is_the_retry() -> None:
    opened = fail(fail(fail(None, T0), T0), T0)
    after = T0 + timedelta(hours=25)
    assert not blocks(opened, after)


def test_a_failed_retry_doubles_the_pause_up_to_the_cap() -> None:
    health = fail(fail(fail(None, T0), T0), T0)
    at = T0
    expected = [48, 96, 168, 168]
    for hours in expected:
        at = pause_end(health)
        health = fail(health, at)
        assert health.paused_until == at + timedelta(hours=hours)


def test_a_successful_retry_goes_back_to_the_base_pause() -> None:
    health = fail(fail(fail(None, T0), T0), T0)
    at = pause_end(health)
    health = fail(health, at)
    assert health.pause_hours == 48
    assert record(health, "workable", True, at, CFG).pause_hours == 24


def test_a_long_retry_after_opens_at_once_for_as_long_as_asked() -> None:
    health = fail(None, T0, retry_after_hours=24.0)
    assert blocks(health, T0) and health.paused_until == T0 + timedelta(hours=24)
    capped = fail(None, T0, retry_after_hours=1000.0)
    assert capped.paused_until == T0 + timedelta(hours=168)


def test_never_fetched_is_unknown() -> None:
    assert status(None, T0) == "unknown"


def test_the_message_is_cut_short() -> None:
    health = fail(None, T0, message="x" * 500)
    assert health.last_message is not None and len(health.last_message) == 200


def _status_error(code: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"retry-after": retry_after} if retry_after else {}
    request = httpx.Request("GET", "https://example.test/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, headers=headers, request=request)
    )


def test_each_error_maps_to_one_reason() -> None:
    assert classify(_status_error(429)) is FailureReason.RATE_LIMITED
    assert classify(_status_error(403)) is FailureReason.FORBIDDEN
    assert classify(_status_error(401)) is FailureReason.FORBIDDEN
    assert classify(_status_error(503)) is FailureReason.SERVER_ERROR
    assert classify(_status_error(404)) is FailureReason.BAD_RESPONSE
    assert classify(httpx.ReadTimeout("slow")) is FailureReason.TIMEOUT
    assert classify(httpx.ConnectError("down")) is FailureReason.UNREACHABLE
    assert classify(KeyError("jobs")) is FailureReason.BAD_RESPONSE


def test_only_a_long_retry_after_counts_as_a_block() -> None:
    assert read_failure(_status_error(429, "86400")).retry_after_hours == 24.0
    assert read_failure(_status_error(429, "2")).retry_after_hours is None
    assert read_failure(_status_error(429)).retry_after_hours is None
    assert read_failure(_status_error(503, "86400")).retry_after_hours is None


def test_a_missing_board_does_not_count_against_the_source() -> None:
    assert read_failure(BoardNotFound("no such board")).counts is False
    assert read_failure(RuntimeError("x")).counts is True


def test_resume_cuts_a_pause_short_and_a_failed_retry_still_doubles_it() -> None:
    paused = fail(fail(fail(None, T0), T0), T0)
    soon = T0 + timedelta(hours=1)
    resumed = resume(paused, soon)
    assert resumed is not None and not blocks(resumed, soon)
    assert status(resumed, soon) == "degraded"
    retry_failed = fail(resumed, soon)
    assert retry_failed.pause_hours == 48
    assert retry_failed.paused_until == soon + timedelta(hours=48)


def test_resume_leaves_a_source_that_is_not_paused_alone() -> None:
    open_ = fail(None, T0)
    assert resume(open_, T0) is open_
    assert resume(None, T0) is None
